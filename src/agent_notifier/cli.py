"""Command-line interface for Agent Notifier."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from aiohttp import ClientSession, UnixConnector

from .agent_commands import format_agent_help, run_agent_command
from .approvals import ApprovalStore
from .acp.server import ACPStdioServer
from .cc_config import configure_project
from .codex.backend import CodexBackend
from .codex.client import CodexAppServerClient
from .codex.sessions import discover_rollout_thread_ids
from .config import NotifierConfig, Paths, initialize_user_config
from .feishu import (
    reply_approval_result_card,
    send_markdown_message,
    send_opencode_approval_card,
    send_opencode_question_card,
    send_text_message,
)
from .hook_config import (
    decode_command,
    default_hooks_path,
    gate_hooks,
    native_stop_hook as build_native_stop_hook_config,
)
from .native_hooks import build_completion_message, claim
from .notification_routing import agent_route_name, resolve_thread_route_name
from .registry import SessionRegistry
from .service import SharedService, approval_details, resolve_codex_binary
from .setup import run_setup
from .versioning import (
    CompatibilityError,
    parse_cc_connect_version,
    parse_codex_version,
    require_supported_version,
)


def _resume_thread_id(codex_args: list[str]) -> str | None:
    try:
        index = codex_args.index("resume")
    except ValueError:
        return None
    if index + 1 >= len(codex_args):
        return None
    candidate = codex_args[index + 1]
    return candidate if candidate and not candidate.startswith("-") else None


async def send_configured_notification(
    paths: Paths, route_name: str, text: str
) -> str:
    config = NotifierConfig.load(paths.config_file)
    route = config.notification_routes.get(route_name)
    if route is None:
        raise ValueError(
            f"notification route {route_name!r} is not configured"
        )
    if not text.strip():
        raise ValueError("notification text is empty")
    sender = (
        send_markdown_message
        if route.message_format == "markdown"
        else send_text_message
    )
    return await sender(
        project=route.project,
        receive_id=route.receive_id,
        receive_id_type=route.receive_id_type,
        text=text,
    )


async def send_opencode_notification(
    paths: Paths, session_id: str, text: str
) -> str:
    """Send an OpenCode notification using its persisted session route."""
    config = NotifierConfig.load(paths.config_file)
    registry = SessionRegistry(paths.state_db)
    try:
        mapping = registry.find_by_thread(session_id)
        route_name = resolve_thread_route_name(
            config, registry, session_id, mapping, fallback="default"
        ) or "default"
    finally:
        registry.close()
    return await send_configured_notification(paths, route_name, text)


def send_opencode_tool_event(paths: Paths, payload_text: str) -> None:
    """Forward one OpenCode tool lifecycle event to the active ACP bridge."""
    payload = json.loads(payload_text)
    if not isinstance(payload, dict):
        raise ValueError("OpenCode tool event must be a JSON object")
    socket_path = paths.runtime_dir / "opencode-tools.sock"
    if not socket_path.exists():
        return
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.sendto(data, str(socket_path))


def bind_opencode_terminal(
    paths: Paths, thread_id: str, route_name: str | None = None
) -> SessionMapping:
    config = NotifierConfig.load(paths.config_file)
    route_name = agent_route_name(config, "opencode", route_name)
    route = config.notification_routes.get(route_name)
    if route is None:
        raise ValueError(f"notification route {route_name!r} is not configured")
    if not route.session_key:
        raise ValueError(
            f"notification route {route_name!r} has no session_key configured"
        )
    registry = SessionRegistry(paths.state_db)
    try:
        mapping = registry.bind(
            "cc_connect",
            route.project,
            route.session_key,
            thread_id,
            os.getcwd(),
        )
        registry.set_thread_route(
            thread_id, "opencode", route_name, mapping.project,
            mapping.external_key, mapping.cwd,
        )
        return mapping
    finally:
        registry.close()


async def opencode_permission(
    paths: Paths,
    session_id: str,
    perm_id: str,
    perm_type: str,
    filepath: str,
    pattern: str = "",
    route_name: str | None = None,
    allow_always: bool = False,
) -> str:
    config = NotifierConfig.load(paths.config_file)
    registry = SessionRegistry(paths.state_db)
    try:
        mapping = registry.find_by_thread(session_id)
        resolved_route = resolve_thread_route_name(
            config,
            registry,
            session_id,
            mapping,
            fallback=route_name or "default",
        )
    finally:
        registry.close()
    route = config.notification_routes.get(resolved_route or route_name)
    if route is None:
        raise ValueError(f"notification route {route_name!r} is not configured")
    options = [{"option_id": "once", "label": "允许一次"}]
    if allow_always:
        options.append({"option_id": "always", "label": "始终允许"})
    options.append({"option_id": "reject", "label": "拒绝"})
    message_id = await send_opencode_approval_card(
        project=route.project,
        receive_id=route.receive_id,
        receive_id_type=route.receive_id_type,
        session_id=session_id,
        perm_id=perm_id,
        perm_type=perm_type,
        filepath=filepath,
        pattern=pattern,
        session_key=route.session_key,
        options=options,
    )
    import json
    meta_path = Path(f"/tmp/oc-perm-msg-{perm_id}.json")
    meta_path.write_text(json.dumps({
        "message_id": message_id,
        "project": route.project,
        "session_id": session_id,
        "perm_type": perm_type,
        "filepath": filepath,
    }))
    return message_id


async def opencode_question(paths: Paths, payload: dict) -> str:
    """Publish a native OpenCode question and persist its answer metadata."""
    session_id = str(payload.get("sessionID", ""))
    request_id = str(payload.get("id", ""))
    questions = payload.get("questions")
    if not session_id or not request_id or not isinstance(questions, list):
        raise ValueError("question payload requires id, sessionID, and questions")
    for path in (
        Path(f"/tmp/oc-question-{request_id}.json"),
        Path(f"/tmp/oc-question-answer-{request_id}.json"),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    config = NotifierConfig.load(paths.config_file)
    registry = SessionRegistry(paths.state_db)
    try:
        mapping = registry.find_by_thread(session_id)
        route_name = resolve_thread_route_name(
            config, registry, session_id, mapping, fallback="default"
        ) or "default"
    finally:
        registry.close()
    route = config.notification_routes.get(route_name)
    if route is None:
        raise ValueError(f"notification route {route_name!r} is not configured")
    message_id = await send_opencode_question_card(
        project=route.project,
        receive_id=route.receive_id,
        receive_id_type=route.receive_id_type,
        session_id=session_id,
        request_id=request_id,
        questions=questions,
        session_key=route.session_key,
    )
    Path(f"/tmp/oc-question-{request_id}.json").write_text(json.dumps({
        "message_id": message_id,
        "project": route.project,
        "session_id": session_id,
        "questions": questions,
    }, ensure_ascii=False))
    return message_id


async def opencode_question_result(
    request_id: str, answers: list[list[str]], status: str = "answered"
) -> str:
    from .feishu import update_opencode_question_card
    meta_path = Path(f"/tmp/oc-question-{request_id}.json")
    if not meta_path.exists():
        raise RuntimeError(f"no OpenCode question found for {request_id}")
    meta = json.loads(meta_path.read_text())
    result = await update_opencode_question_card(
        project=meta["project"],
        message_id=meta["message_id"],
        request_id=request_id,
        session_id=meta["session_id"],
        questions=meta["questions"],
        answers=answers,
        status=status,
    )
    meta_path.unlink(missing_ok=True)
    Path(f"/tmp/oc-question-answer-{request_id}.json").unlink(missing_ok=True)
    return result


def opencode_question_select(request_id: str, question_index: int, option_index: int) -> None:
    """Record one Feishu option selection for a native OpenCode question."""
    meta_path = Path(f"/tmp/oc-question-{request_id}.json")
    if not meta_path.exists():
        raise RuntimeError(f"no OpenCode question found for {request_id}")
    meta = json.loads(meta_path.read_text())
    questions = meta.get("questions", [])
    if not 0 <= question_index < len(questions):
        raise ValueError("question index out of range")
    options = questions[question_index].get("options", [])
    if not 0 <= option_index < len(options):
        raise ValueError("option index out of range")
    answer_path = Path(f"/tmp/oc-question-answer-{request_id}.json")
    answers = [[] for _ in questions]
    if answer_path.exists():
        answers = json.loads(answer_path.read_text()).get("answers", answers)
    if questions[question_index].get("multiple", False):
        value = str(options[option_index].get("label", ""))
        if value not in answers[question_index]:
            answers[question_index].append(value)
    else:
        answers[question_index] = [
            str(options[option_index].get("label", ""))
        ]
    temporary = answer_path.with_suffix(".json.tmp")
    auto_submit = len(questions) == 1 and not questions[question_index].get("multiple", False)
    temporary.write_text(json.dumps({
        "answers": answers,
        "submitted": auto_submit,
    }, ensure_ascii=False))
    os.replace(temporary, answer_path)


def opencode_question_submit(request_id: str) -> None:
    answer_path = Path(f"/tmp/oc-question-answer-{request_id}.json")
    if not answer_path.exists():
        raise RuntimeError(f"no OpenCode question answers found for {request_id}")
    answer = json.loads(answer_path.read_text())
    answers = answer.get("answers", [])
    if not answers or not all(isinstance(item, list) and item for item in answers):
        raise RuntimeError("answer every OpenCode question before submitting")
    temporary = answer_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"answers": answers, "submitted": True}, ensure_ascii=False))
    os.replace(temporary, answer_path)


async def opencode_reply_result(
    paths: Paths,
    perm_id: str,
    decision: str,
) -> str:
    from .feishu import update_opencode_approval_card
    import json
    meta_path = Path(f"/tmp/oc-perm-msg-{perm_id}.json")
    if not meta_path.exists():
        raise RuntimeError(f"no card metadata found for {perm_id}")
    meta = json.loads(meta_path.read_text())
    response = {
        "allow": "once",
        "deny": "reject",
        "once": "once",
        "always": "always",
        "reject": "reject",
        "neutral": "neutral",
    }[decision]
    if response != "neutral":
        if not meta.get("session_id"):
            raise RuntimeError(
                f"approval metadata for {perm_id} has no OpenCode session_id"
            )
        # OpenCode emits permission.replied immediately after this API call.
        # Tell the plugin that this resolution originated from Feishu so its
        # terminal-side neutral update cannot overwrite the result card.
        Path(f"/tmp/oc-perm-feishu-{perm_id}.marker").touch()
        Path(
            f"/tmp/oc-perm-feishu-session-{meta['session_id']}.marker"
        ).touch()
        await opencode_decide(
            meta["session_id"],
            perm_id,
            response,
            os.environ.get("AGENT_NOTIFIER_OPENCODE_URL", "http://127.0.0.1:4098"),
        )
    result = await update_opencode_approval_card(
        project=meta["project"],
        message_id=meta["message_id"],
        perm_type=meta.get("perm_type", "unknown"),
        filepath=meta.get("filepath", ""),
        decision=response,
    )
    try:
        meta_path.unlink()
    except Exception:
        pass
    return result


async def opencode_decide(
    session_id: str,
    perm_id: str,
    decision: str,
    base_url: str,
) -> None:
    from aiohttp import ClientSession

    response_map = {"allow": "once", "deny": "reject"}
    response = response_map.get(decision, decision)
    async with ClientSession() as session:
        async with session.post(
            f"{base_url}/session/{session_id}/permissions/{perm_id}",
            json={"response": response},
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"opencode API error: {resp.status} {text}")


def native_stop_hook(paths: Paths, payload_text: str) -> int:
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return 0
    result = build_completion_message(payload)
    if result is None:
        return 0
    message, dedupe_key = result
    if not claim(paths.state_dir / "native_hook_seen", dedupe_key):
        return 0
    config = NotifierConfig.load(paths.config_file)
    registry = SessionRegistry(paths.state_db)
    try:
        thread_id = (
            payload.get("thread-id")
            or payload.get("thread_id")
            or payload.get("threadId")
            or payload.get("session_id")
            or payload.get("sessionId")
        )
        mapping = registry.find_by_thread(str(thread_id)) if thread_id else None
        route_name = resolve_thread_route_name(
            config,
            registry,
            str(thread_id) if thread_id else None,
            mapping,
            fallback="default",
        ) or "default"
    finally:
        registry.close()
    asyncio.run(send_configured_notification(paths, route_name, message))
    return 0


def bind_terminal_thread(
    paths: Paths,
    thread_id: str,
    project: str | None = None,
    cwd: str | None = None,
    notification_route: str | None = None,
):
    registry = SessionRegistry(paths.state_db)
    try:
        config = NotifierConfig.load(paths.config_file)
        notification_route = agent_route_name(config, "codex", notification_route)
        configured = config.notification_routes.get(notification_route)
        if configured and (project is None or project == configured.project):
            # A notification route is intentionally allowed to have no
            # interactive cc-connect session. Use a stable chat-scoped key
            # so terminal-origin turns can still be indexed by thread.
            external_key = (
                f"feishu:{configured.receive_id}:terminal:{thread_id}"
            )
            mapping = registry.subscribe(
                "cc_connect",
                configured.project,
                external_key,
                thread_id,
                cwd or os.getcwd(),
            )
            registry.set_thread_route(
                thread_id, "codex", notification_route, mapping.project,
                mapping.external_key, mapping.cwd,
            )
            return mapping
        routes = registry.list_routes("cc_connect", project)
        scope = f"project {project!r}" if project else "all configured projects"
        if not routes:
            raise ValueError(f"no Feishu route is registered for {scope}")
        if len(routes) > 1:
            projects = ", ".join(sorted({route.project for route in routes}))
            raise ValueError(
                f"multiple Feishu routes are registered for {scope} "
                f"(projects: {projects}); specify --notify-project"
            )
        route = routes[0]
        mapping = registry.subscribe(
            route.adapter,
            route.project,
            route.external_key,
            thread_id,
            cwd or route.cwd,
        )
        registry.set_thread_route(
            thread_id, "codex", notification_route, mapping.project,
            mapping.external_key, mapping.cwd,
        )
        return mapping
    finally:
        registry.close()


def activate_terminal_thread(
    paths: Paths,
    thread_id: str,
    project: str | None = None,
):
    """Select one subscribed thread as the target for incoming chat tasks."""
    registry = SessionRegistry(paths.state_db)
    try:
        subscriptions = registry.list_subscriptions_by_thread(
            thread_id, project=project
        )
        if not subscriptions:
            scope = f" in project {project!r}" if project else ""
            raise ValueError(
                f"thread {thread_id!r} has no Feishu notification subscription{scope}"
            )
        if len(subscriptions) > 1:
            projects = ", ".join(
                sorted({subscription.project for subscription in subscriptions})
            )
            raise ValueError(
                f"thread {thread_id!r} has multiple Feishu subscriptions "
                f"(projects: {projects}); specify --project"
            )
        route = subscriptions[0]
        mapping = registry.bind(
            route.adapter,
            route.project,
            route.external_key,
            route.thread_id,
            route.cwd,
        )
        registry.set_active_agent(
            route.project, route.external_key, "codex"
        )
        return mapping
    finally:
        registry.close()


def _agent_adapter(provider: str) -> str:
    normalized = provider.lower()
    if normalized != "codex":
        raise ValueError(
            f"unsupported agent provider {provider!r}; supported: codex"
        )
    return "cc_connect"


def _agent_provider_label(provider: str) -> str:
    return {"codex": "Codex"}.get(provider.lower(), provider)


def list_agent_sessions(
    paths: Paths,
    provider: str,
    project: str | None = None,
    external_key: str | None = None,
) -> str:
    adapter = _agent_adapter(provider)
    registry = SessionRegistry(paths.state_db)
    try:
        registry.prune_missing_threads(
            adapter,
            discover_rollout_thread_ids(),
            project=project,
            external_key=external_key,
        )
        subscriptions = registry.list_subscriptions(
            adapter, project=project, external_key=external_key
        )
        active_routes = registry.list_routes(adapter, project=project)
        if external_key is not None:
            active_routes = [
                route
                for route in active_routes
                if route.external_key == external_key
            ]
    finally:
        registry.close()

    active = {
        (route.project, route.external_key, route.thread_id)
        for route in active_routes
    }
    seen = set()
    sessions = []
    ordered = sorted(
        subscriptions,
        key=lambda session: (
            session.project,
            session.external_key,
            session.thread_id,
        )
        not in active,
    )
    for session in ordered:
        key = (session.project, session.external_key, session.thread_id)
        if key in seen:
            continue
        seen.add(key)
        marker = "当前" if key in active else "可用"
        sessions.append(
            f"[{marker}] {session.short_thread_id}\n"
            f"目录：{session.cwd}"
        )
    title = f"{_agent_provider_label(provider)} 会话（{len(sessions)}）"
    if not sessions:
        return f"{title}\n\n（暂无已订阅会话）"
    return f"{title}\n\n" + "\n\n".join(sessions) + (
        f"\n\n切换：\n/agent-switch {provider.lower()} <短ID>"
    )


def current_agent_sessions(
    paths: Paths,
    project: str | None = None,
    external_key: str | None = None,
) -> str:
    registry = SessionRegistry(paths.state_db)
    try:
        if project and external_key:
            active = registry.get_active_agent(project, external_key)
            provider = active.provider if active else "codex"
            adapter = _agent_adapter(provider)
            route = registry.get(adapter, project, external_key)
            routes = [route] if route else []
        else:
            provider = "codex"
            routes = registry.list_routes("cc_connect", project=project)
    finally:
        registry.close()
    if external_key is not None:
        routes = [
            route for route in routes if route.external_key == external_key
        ]

    lines = []
    if not routes:
        lines.append(f"类型：{provider}\n会话：（未绑定）")
    else:
        for route in routes:
            lines.append(
                f"类型：{provider}\n"
                f"会话：{route.short_thread_id}\n"
                f"目录：{route.cwd}"
            )
    return "当前 Agent 会话\n\n" + "\n\n".join(lines)


async def create_agent_session(
    paths: Paths,
    provider: str,
    project: str | None,
    external_key: str | None,
):
    adapter = _agent_adapter(provider)
    if not project or not external_key:
        raise ValueError(
            "agent-new requires a cc-connect project and session key"
        )

    registry = SessionRegistry(paths.state_db)
    try:
        current = registry.get(adapter, project, external_key)
        cwd = current.cwd if current else os.getcwd()
    finally:
        registry.close()

    rpc = CodexAppServerClient(
        paths.proxy_socket, "agent-notifier-bootstrap"
    )
    await rpc.connect()
    try:
        result = await rpc.call(
            "thread/start",
            {
                "cwd": cwd,
                "approvalPolicy": "on-request",
                "threadSource": "user",
            },
        )
        thread_id = result["thread"]["id"]
        turn_result = await rpc.call(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": "初始化会话，只回复 OK。",
                        "text_elements": [],
                    }
                ],
            },
        )
        turn_id = turn_result["turn"]["id"]
        while True:
            event = await asyncio.wait_for(rpc.next_event(), timeout=120)
            if event.get("method") != "turn/completed":
                continue
            params = event.get("params") or {}
            turn = params.get("turn") or {}
            if (
                params.get("threadId") != thread_id
                or turn.get("id") != turn_id
            ):
                continue
            if turn.get("status") != "completed":
                error = turn.get("error") or {}
                raise RuntimeError(
                    error.get("message")
                    or f"Codex bootstrap turn ended with status "
                    f"{turn.get('status')!r}"
                )
            break
    finally:
        await rpc.close()

    registry = SessionRegistry(paths.state_db)
    try:
        mapping = registry.bind(
            adapter, project, external_key, thread_id, cwd
        )
        registry.set_active_agent(project, external_key, provider.lower())
        return mapping
    finally:
        registry.close()


def format_agent_new(provider: str, mapping) -> str:
    return (
        "Agent 会话已新建\n\n"
        f"类型：{provider.lower()}\n"
        f"会话：{mapping.short_thread_id}\n"
        f"目录：{mapping.cwd}"
    )


def switch_agent_session(
    paths: Paths,
    provider: str,
    session_id: str,
    project: str | None = None,
    external_key: str | None = None,
):
    adapter = _agent_adapter(provider)
    registry = SessionRegistry(paths.state_db)
    try:
        subscriptions = registry.list_subscriptions(
            adapter, project=project, external_key=external_key
        )
        exact = [
            route for route in subscriptions if route.thread_id == session_id
        ]
        matches = exact or [
            route
            for route in subscriptions
            if route.thread_id.startswith(session_id)
        ]
        unique = {
            (route.project, route.external_key, route.thread_id): route
            for route in matches
        }
        if not unique:
            raise ValueError(
                f"no subscribed {provider} session matches {session_id!r}"
            )
        if len(unique) > 1:
            raise ValueError(
                f"ambiguous {provider} session prefix {session_id!r}; "
                "use a longer session ID"
            )
        route = next(iter(unique.values()))
        mapping = registry.bind(
            route.adapter,
            route.project,
            route.external_key,
            route.thread_id,
            route.cwd,
        )
        registry.set_active_agent(
            route.project, route.external_key, provider.lower()
        )
        return mapping
    finally:
        registry.close()


def format_agent_switch(provider: str, mapping) -> str:
    return (
        "Agent 会话已切换\n\n"
        f"类型：{provider.lower()}\n"
        f"会话：{mapping.short_thread_id}\n"
        f"目录：{mapping.cwd}"
    )


async def decide_approval(paths: Paths, decision: str, approval_id: str) -> str:
    connector = UnixConnector(path=str(paths.proxy_socket))
    async with ClientSession(connector=connector) as session:
        async with session.post(
            "http://localhost/approval",
            json={"decision": decision, "approval_id": approval_id},
        ) as response:
            payload = await response.json()
            if response.status >= 400:
                raise ValueError(payload.get("error") or "approval decision failed")
            status = str(payload.get("status") or "resolved")
    await reply_approval_decision(paths, decision, approval_id, status)
    return status


async def reply_approval_decision(
    paths: Paths,
    decision: str,
    approval_id: str,
    status: str,
) -> None:
    store = ApprovalStore(paths.state_db)
    try:
        record = store.find_by_prefix(approval_id)
    finally:
        store.close()
    if record is None:
        raise ValueError(f"approval record not found: {approval_id}")
    if not record.feishu_message_id:
        raise ValueError(f"approval has no Feishu message id: {approval_id}")
    registry = SessionRegistry(paths.state_db)
    try:
        mapping = registry.find_by_thread(record.thread_id)
    finally:
        registry.close()
    if mapping is None:
        raise ValueError(f"approval thread has no Feishu route: {record.thread_id}")
    reason, operation = approval_details(record.payload)
    await reply_approval_result_card(
        project=mapping.project,
        message_id=record.feishu_message_id,
        approval_id=approval_id,
        decision=decision,
        status=status,
        session_label=mapping.session_label,
        thread_id=record.thread_id,
        cwd=mapping.cwd,
        reason=reason,
        operation=operation,
    )


def approval_decision_message(
    status: str,
    decision: str,
    approval_id: str,
    quiet: bool,
    now: datetime | None = None,
) -> str | None:
    if quiet:
        return None
    current = now or datetime.now().astimezone()
    return current.strftime("%Y-%m-%d %H:%M")


def _run_version(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"failed: {' '.join(command)}")
    return result.stdout + result.stderr


def _cc_connect_binary() -> str:
    configured = os.environ.get("AGENT_NOTIFIER_CC_CONNECT")
    if configured:
        path = Path(configured).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"configured cc-connect does not exist: {path}")
        return str(path)
    direct = Path.home() / ".npm-global/lib/node_modules/cc-connect/bin/cc-connect"
    if direct.exists():
        return str(direct)
    found = shutil.which("cc-connect")
    if found:
        return found
    raise FileNotFoundError("cc-connect is not installed")


def check_versions(
    require_cc: bool = True,
    codex_binary: str = "codex",
    require_codex: bool = True,
) -> tuple[str, str | None]:
    codex_version = "not checked"
    if require_codex:
        codex_version = parse_codex_version(_run_version([codex_binary, "--version"]))
        require_supported_version("codex", codex_version)
    cc_version = None
    if require_cc:
        cc_version = parse_cc_connect_version(
            _run_version([_cc_connect_binary(), "--version"])
        )
        require_supported_version("cc-connect", cc_version)
    return codex_version, cc_version


def _socket_ready(path: Path) -> bool:
    try:
        if not stat.S_ISSOCK(path.stat().st_mode):
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(str(path))
        return True
    except (FileNotFoundError, ConnectionError, OSError):
        return False


def _wait_for_service(paths: Paths, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _socket_ready(paths.proxy_socket):
            return True
        time.sleep(0.1)
    return False


def ensure_service(paths: Paths, timeout: float = 20) -> None:
    if _socket_ready(paths.proxy_socket):
        return
    systemctl = shutil.which("systemctl")
    if systemctl:
        started = subprocess.run(
            [systemctl, "--user", "start", "agent-notifier.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
        if started and _wait_for_service(paths, min(timeout, 10)):
            return
    if not _socket_ready(paths.proxy_socket):
        paths.ensure_directories()
        log_path = paths.log_dir / "service.log"
        log = log_path.open("a")
        subprocess.Popen(
            [sys.executable, "-m", "agent_notifier.cli", "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        log.close()
    if _wait_for_service(paths, timeout):
        return
    raise TimeoutError(f"agent-notifier service did not start; see {paths.log_dir}")


async def run_acp(paths: Paths) -> None:
    ensure_service(paths)
    rpc = CodexAppServerClient(paths.proxy_socket, "cc_connect")
    await rpc.connect()
    registry = SessionRegistry(paths.state_db)
    config = NotifierConfig.load(paths.config_file)
    backend = CodexBackend(rpc, registry, config.progress)
    server = ACPStdioServer(backend, sys.stdin, sys.stdout, registry)
    rpc.set_server_request_handler(server.handle_codex_request)
    try:
        await server.run()
    finally:
        await backend.close()
        registry.close()
        await rpc.close()


async def run_acp_opencode(paths: Paths, base_url: str) -> None:
    from .opencode.backend import OpencodeBackend
    from .opencode.client import OpencodeClient

    client = OpencodeClient(base_url, directory=os.getcwd())
    await client.connect()
    await client.start_sse()
    registry = SessionRegistry(paths.state_db)
    config = NotifierConfig.load(paths.config_file)
    server_ref: dict = {}

    async def request_permission(params: dict) -> dict:
        server = server_ref.get("server")
        if server is None:
            return {"outcome": {"outcome": "cancelled"}}
        return await server.call_client("session/request_permission", params)

    backend = OpencodeBackend(
        client,
        registry,
        request_permission,
        config.progress,
        paths.runtime_dir / "opencode-tools.sock",
    )
    server = ACPStdioServer(backend, sys.stdin, sys.stdout, registry)
    server_ref["server"] = server
    try:
        await server.run()
    finally:
        await backend.close()
        registry.close()


async def run_acp_multi(paths: Paths, base_url: str) -> None:
    from .multi_backend import MultiBackendDispatcher, _load_chat_routes
    from .opencode.backend import OpencodeBackend
    from .opencode.client import OpencodeClient

    ensure_service(paths)
    codex_rpc = CodexAppServerClient(paths.proxy_socket, "cc_connect")
    await codex_rpc.connect()

    opencode_client = OpencodeClient(base_url, directory=os.getcwd())
    await opencode_client.connect()
    await opencode_client.start_sse()

    registry = SessionRegistry(paths.state_db)
    config = NotifierConfig.load(paths.config_file)
    chat_routes = _load_chat_routes(paths.config_file)
    server_ref: dict = {}

    async def request_permission(params: dict) -> dict:
        server = server_ref.get("server")
        if server is None:
            return {"outcome": {"outcome": "cancelled"}}
        return await server.call_client("session/request_permission", params)

    codex_backend = CodexBackend(codex_rpc, registry, config.progress)
    opencode_backend = OpencodeBackend(
        opencode_client,
        registry,
        request_permission,
        config.progress,
        paths.runtime_dir / "opencode-tools.sock",
    )

    dispatcher = MultiBackendDispatcher(
        codex_backend,
        opencode_backend,
        registry,
        chat_routes,
        config_path=paths.config_file,
    )

    server = ACPStdioServer(dispatcher, sys.stdin, sys.stdout, registry)
    codex_rpc.set_server_request_handler(server.handle_codex_request)
    server_ref["server"] = server

    try:
        await server.run()
    finally:
        await dispatcher.close()
        registry.close()
        await codex_rpc.close()


def configure_cc(paths: Paths, project: str, config_path: Path) -> Path:
    paths.ensure_directories()
    source = config_path.read_text()
    command = shutil.which("agent-notifier") or str(Path(sys.argv[0]).resolve())
    config = NotifierConfig.load(paths.config_file)
    updated = configure_project(source, project, command, config.progress)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup = config_path.with_name(f"{config_path.name}.agent-notifier.{timestamp}.bak")
    shutil.copy2(config_path, backup)
    temporary = config_path.with_suffix(config_path.suffix + ".agent-notifier.tmp")
    temporary.write_text(updated)
    os.replace(temporary, config_path)
    (paths.config_dir / "last_cc_connect_backup").write_text(str(backup))
    return backup


def restore_cc(paths: Paths, config_path: Path) -> Path:
    marker = paths.config_dir / "last_cc_connect_backup"
    if not marker.exists():
        raise FileNotFoundError("no cc-connect backup recorded")
    backup = Path(marker.read_text().strip())
    if not backup.exists():
        raise FileNotFoundError(f"backup no longer exists: {backup}")
    shutil.copy2(backup, config_path)
    return backup


def configure_hooks(paths: Paths, hooks_path: Path) -> Path | None:
    if not hooks_path.exists():
        return None
    paths.ensure_directories()
    executable = shutil.which("agent-notifier") or str(Path(sys.argv[0]).resolve())
    updated, changed = gate_hooks(hooks_path.read_text(), executable)
    if changed == 0:
        return None
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup = hooks_path.with_name(f"{hooks_path.name}.agent-notifier.{timestamp}.bak")
    shutil.copy2(hooks_path, backup)
    temporary = hooks_path.with_suffix(hooks_path.suffix + ".agent-notifier.tmp")
    temporary.write_text(updated)
    os.replace(temporary, hooks_path)
    (paths.config_dir / "last_codex_hooks_backup").write_text(str(backup))
    return backup


def configure_native_hooks(paths: Paths, hooks_path: Path) -> Path | None:
    paths.ensure_directories()
    executable = shutil.which("agent-notifier") or str(Path(sys.argv[0]).resolve())
    source = hooks_path.read_text() if hooks_path.exists() else '{"hooks": {}}\n'
    updated, changed = build_native_stop_hook_config(source, executable)
    if not changed:
        return None
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup = hooks_path.with_name(
        f"{hooks_path.name}.agent-notifier.{timestamp}.bak"
    )
    if hooks_path.exists():
        shutil.copy2(hooks_path, backup)
    temporary = hooks_path.with_suffix(hooks_path.suffix + ".agent-notifier.tmp")
    temporary.write_text(updated)
    os.replace(temporary, hooks_path)
    (paths.config_dir / "last_codex_hooks_backup").write_text(str(backup))
    return backup


def restore_hooks(paths: Paths, hooks_path: Path) -> Path:
    marker = paths.config_dir / "last_codex_hooks_backup"
    if not marker.exists():
        raise FileNotFoundError("no Codex hooks backup recorded")
    backup = Path(marker.read_text().strip())
    if not backup.exists():
        raise FileNotFoundError(f"backup no longer exists: {backup}")
    shutil.copy2(backup, hooks_path)
    return backup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-notifier")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the shared background service")
    serve.add_argument(
        "--codex",
        default=os.environ.get("AGENT_NOTIFIER_CODEX", "codex"),
        help="Codex CLI executable used for the managed App Server",
    )
    acp = sub.add_parser("acp", help="run the cc-connect ACP stdio adapter")
    acp.add_argument(
        "--backend",
        choices=("codex", "opencode", "multi"),
        default="codex",
        help="agent backend implementation (default: codex)",
    )
    acp.add_argument(
        "--opencode-url",
        default=os.environ.get(
            "AGENT_NOTIFIER_OPENCODE_URL", "http://127.0.0.1:4098"
        ),
        help="opencode serve HTTP base URL (for --backend opencode)",
    )
    codex = sub.add_parser("codex", help="launch native Codex TUI through the service")
    codex.add_argument(
        "--notify-project",
        help="Feishu project to bind when resuming a Codex thread",
    )
    codex.add_argument(
        "--notify-route",
        default=None,
        help="configured outbound notification route (default: agent_routes.codex)",
    )
    codex.add_argument("codex_args", nargs=argparse.REMAINDER)
    bind = sub.add_parser(
        "bind", help="bind an existing Codex thread to a Feishu project"
    )
    bind.add_argument("thread_id")
    bind.add_argument("--project", required=True)
    bind_terminal = sub.add_parser(
        "bind-terminal",
        help="register an opencode terminal session for cc-connect card recognition",
    )
    bind_terminal.add_argument("--thread-id", required=True)
    bind_terminal.add_argument("--route", default=None)
    activate = sub.add_parser(
        "activate", help="select a subscribed Codex thread for incoming chat tasks"
    )
    activate.add_argument("thread_id")
    activate.add_argument("--project")
    agent_list = sub.add_parser(
        "agent-list", help="list subscribed sessions for one coding agent"
    )
    agent_list.add_argument("provider")
    agent_list.add_argument("--project")
    agent_list.add_argument("--external-key")
    agent_current = sub.add_parser(
        "agent-current", help="show current coding-agent task targets"
    )
    agent_current.add_argument("--project")
    agent_current.add_argument("--external-key")
    agent_new = sub.add_parser(
        "agent-new", help="create and activate a new coding-agent session"
    )
    agent_new.add_argument("provider")
    agent_new.add_argument("--project")
    agent_new.add_argument("--external-key")
    agent_switch = sub.add_parser(
        "agent-switch", help="switch one coding agent to a subscribed session"
    )
    agent_switch.add_argument("provider")
    agent_switch.add_argument("session_id")
    agent_switch.add_argument("--project")
    agent_switch.add_argument("--external-key")
    agent_cmd = sub.add_parser(
        "agent-cmd", help="run a safe read-only command on the active agent"
    )
    agent_cmd.add_argument(
        "agent_command", choices=("status", "model", "usage", "session")
    )
    agent_cmd.add_argument("--project")
    agent_cmd.add_argument("--external-key")
    sub.add_parser("agent-help", help="show Agent Notifier chat commands")
    notify = sub.add_parser(
        "notify", help="send a message to a configured notification route"
    )
    notify.add_argument("--route", default="default")
    notify.add_argument(
        "-m", "--message",
        help="notification body as a command-line argument",
    )
    notify.add_argument(
        "--stdin",
        action="store_true",
        help="read the notification body from stdin",
    )
    opencode_notify = sub.add_parser(
        "opencode-notify",
        help="send a notification using an OpenCode session's configured route",
    )
    opencode_notify.add_argument("--session", required=True)
    opencode_notify.add_argument("-m", "--message")
    opencode_notify.add_argument("--stdin", action="store_true")
    tool_event = sub.add_parser(
        "opencode-tool-event", help="forward an OpenCode tool lifecycle event"
    )
    tool_event.add_argument(
        "--stdin", action="store_true", required=True,
        help="read the event JSON from stdin",
    )
    oc_perm = sub.add_parser(
        "opencode-permission", help="send an interactive opencode approval card"
    )
    oc_perm.add_argument("--session", required=True)
    oc_perm.add_argument("--perm", required=True)
    oc_perm.add_argument("--type", default="unknown")
    oc_perm.add_argument("--path", default="")
    oc_perm.add_argument("--pattern", default="")
    oc_perm.add_argument(
        "--allow-always", action="store_true",
        help="include OpenCode's durable allow choice",
    )
    oc_perm.add_argument(
        "--route",
        default=None,
        help="configured outbound notification route (default: agent_routes.opencode)",
    )
    oc_question = sub.add_parser(
        "opencode-question", help="send an OpenCode question choice card"
    )
    oc_question.add_argument("--stdin", action="store_true", required=True)
    oc_question_select = sub.add_parser(
        "opencode-question-select", help="record an OpenCode question choice"
    )
    oc_question_select.add_argument("request_id")
    oc_question_select.add_argument("question_index", type=int)
    oc_question_select.add_argument("option_index", type=int)
    oc_question_submit = sub.add_parser(
        "opencode-question-submit", help="submit OpenCode question choices"
    )
    oc_question_submit.add_argument("request_id")
    oc_question_result = sub.add_parser(
        "opencode-question-result", help="update an answered OpenCode question card"
    )
    oc_question_result.add_argument("--request", required=True)
    oc_question_result.add_argument("--stdin", action="store_true", required=True)
    oc_question_result.add_argument(
        "--status", choices=("answered", "terminal", "rejected"), default="answered"
    )
    oc_decide = sub.add_parser(
        "opencode-decide", help="respond to an opencode permission request"
    )
    oc_decide.add_argument("--session", required=True)
    oc_decide.add_argument("--perm", required=True)
    oc_decide.add_argument("decision", choices=("allow", "deny"))
    oc_decide.add_argument(
        "--opencode-url",
        default=os.environ.get("AGENT_NOTIFIER_OPENCODE_URL", "http://127.0.0.1:4098"),
    )
    oc_reply = sub.add_parser(
        "opencode-reply-result", help="update an opencode approval card with the result"
    )
    oc_reply.add_argument("--perm", required=True)
    oc_reply.add_argument(
        "decision",
        choices=("allow", "deny", "once", "always", "reject", "neutral"),
    )
    native_hook = sub.add_parser(
        "native-hook", help="handle a native Codex lifecycle hook"
    )
    native_hook.add_argument("hook_name", choices=("stop",))
    decide = sub.add_parser(
        "decide", help="resolve a pending Codex approval request"
    )
    decide.add_argument(
        "--quiet",
        action="store_true",
        help="suppress success output for interactive card callbacks",
    )
    decide.add_argument("decision", choices=("allow", "deny"))
    decide.add_argument("approval_id")
    sub.add_parser("status", help="show service status")
    doctor = sub.add_parser("doctor", help="validate versions, paths, and service")
    doctor.add_argument(
        "--strict",
        action="store_true",
        help="also require cc-connect, configured notification routes, and OpenCode",
    )
    setup = sub.add_parser(
        "setup", help="configure local chat routes without storing credentials"
    )
    setup.add_argument("--project")
    setup.add_argument("--interactive-chat-id")
    setup.add_argument("--notification-chat-id")
    setup.add_argument("--non-interactive", action="store_true")
    sub.add_parser(
        "init-config",
        help="create the default Agent Notifier config if it is missing",
    )
    logs = sub.add_parser("logs", help="follow service logs")
    logs.add_argument("--no-follow", action="store_true")
    configure = sub.add_parser("configure-cc", help="configure one cc-connect project")
    configure.add_argument("--project", required=True)
    configure.add_argument("--config", type=Path, default=Path.home() / ".cc-connect/config.toml")
    restore = sub.add_parser("restore-cc", help="restore the last cc-connect backup")
    restore.add_argument("--config", type=Path, default=Path.home() / ".cc-connect/config.toml")
    hooks = sub.add_parser("configure-hooks", help="gate duplicate Codex notification hooks")
    hooks.add_argument("--hooks", type=Path, default=default_hooks_path())
    native_hooks = sub.add_parser(
        "configure-native-hooks",
        help="install the native Codex Stop notification hook",
    )
    native_hooks.add_argument("--hooks", type=Path, default=default_hooks_path())
    restore_hooks_parser = sub.add_parser(
        "restore-hooks", help="restore the last Codex hooks backup"
    )
    restore_hooks_parser.add_argument("--hooks", type=Path, default=default_hooks_path())
    gate = sub.add_parser("hook-gate", help=argparse.SUPPRESS)
    gate.add_argument("--encoded", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    paths = Paths.from_environment()
    try:
        if args.command == "serve":
            check_versions(require_cc=False, codex_binary=args.codex)
            asyncio.run(SharedService(paths, codex_binary=args.codex).run())
        elif args.command == "acp":
            check_versions(require_cc=True)
            if args.backend == "opencode":
                asyncio.run(run_acp_opencode(paths, args.opencode_url))
            elif args.backend == "multi":
                asyncio.run(run_acp_multi(paths, args.opencode_url))
            else:
                asyncio.run(run_acp(paths))
        elif args.command == "codex":
            codex_binary = resolve_codex_binary()
            check_versions(
                require_cc=False,
                codex_binary=codex_binary,
            )
            ensure_service(paths)
            thread_id = _resume_thread_id(args.codex_args)
            if args.notify_project and thread_id is None:
                raise ValueError(
                    "--notify-project requires `agent-notifier codex "
                    "[--notify-project PROJECT] resume THREAD_ID`"
                )
            if thread_id:
                mapping = bind_terminal_thread(
                    paths,
                    thread_id,
                    project=args.notify_project,
                    cwd=os.getcwd(),
                    notification_route=args.notify_route,
                )
                print(
                    f"Feishu notifications: {mapping.project} "
                    f"<- {mapping.thread_id}",
                    file=sys.stderr,
                )
            os.execv(
                codex_binary,
                [codex_binary, "--remote", f"unix://{paths.proxy_socket}", *args.codex_args],
            )
        elif args.command == "bind":
            mapping = bind_terminal_thread(
                paths,
                args.thread_id,
                project=args.project,
                cwd=os.getcwd(),
            )
            print(f"bound: {mapping.project} <- {mapping.thread_id}")
        elif args.command == "bind-terminal":
            mapping = bind_opencode_terminal(paths, args.thread_id, args.route)
            print(f"bound: {mapping.project} <- {mapping.thread_id}")
        elif args.command == "activate":
            mapping = activate_terminal_thread(
                paths,
                args.thread_id,
                project=args.project,
            )
            print(f"active: {mapping.project} -> {mapping.thread_id}")
        elif args.command == "agent-list":
            print(
                list_agent_sessions(
                    paths,
                    args.provider,
                    project=args.project or os.environ.get("CC_PROJECT"),
                    external_key=(
                        args.external_key or os.environ.get("CC_SESSION_KEY")
                    ),
                )
            )
        elif args.command == "agent-current":
            print(
                current_agent_sessions(
                    paths,
                    project=args.project or os.environ.get("CC_PROJECT"),
                    external_key=(
                        args.external_key or os.environ.get("CC_SESSION_KEY")
                    ),
                )
            )
        elif args.command == "agent-new":
            ensure_service(paths)
            mapping = asyncio.run(
                create_agent_session(
                    paths,
                    args.provider,
                    project=args.project or os.environ.get("CC_PROJECT"),
                    external_key=(
                        args.external_key or os.environ.get("CC_SESSION_KEY")
                    ),
                )
            )
            print(format_agent_new(args.provider, mapping))
        elif args.command == "agent-switch":
            mapping = switch_agent_session(
                paths,
                args.provider,
                args.session_id,
                project=args.project or os.environ.get("CC_PROJECT"),
                external_key=(
                    args.external_key or os.environ.get("CC_SESSION_KEY")
                ),
            )
            print(format_agent_switch(args.provider, mapping))
        elif args.command == "agent-cmd":
            ensure_service(paths)
            print(
                asyncio.run(
                    run_agent_command(
                        paths,
                        args.agent_command,
                        project=args.project or os.environ.get("CC_PROJECT"),
                        external_key=(
                            args.external_key
                            or os.environ.get("CC_SESSION_KEY")
                        ),
                    )
                )
            )
        elif args.command == "agent-help":
            print(format_agent_help())
        elif args.command == "notify":
            if args.message is not None:
                text = args.message
            elif args.stdin:
                text = sys.stdin.read()
            else:
                raise ValueError("notify requires -m <message> or --stdin")
            message_id = asyncio.run(
                send_configured_notification(
                    paths, args.route, text
                )
            )
            print(f"sent: {message_id}")
        elif args.command == "opencode-notify":
            if args.message is not None:
                text = args.message
            elif args.stdin:
                text = sys.stdin.read()
            else:
                raise ValueError(
                    "opencode-notify requires -m <message> or --stdin"
                )
            message_id = asyncio.run(
                send_opencode_notification(paths, args.session, text)
            )
            print(f"sent: {message_id}")
        elif args.command == "opencode-tool-event":
            send_opencode_tool_event(paths, sys.stdin.read())
        elif args.command == "opencode-permission":
            message_id = asyncio.run(
                opencode_permission(
                    paths, args.session, args.perm, args.type, args.path,
                    args.pattern, args.route, args.allow_always
                )
            )
            print(f"sent: {message_id}")
        elif args.command == "opencode-question":
            payload = json.loads(sys.stdin.read())
            message_id = asyncio.run(opencode_question(paths, payload))
            print(f"sent: {message_id}")
        elif args.command == "opencode-question-select":
            opencode_question_select(
                args.request_id, args.question_index, args.option_index
            )
            print("recorded")
        elif args.command == "opencode-question-submit":
            opencode_question_submit(args.request_id)
            print("submitted")
        elif args.command == "opencode-question-result":
            payload = json.loads(sys.stdin.read())
            asyncio.run(opencode_question_result(
                args.request, payload.get("answers", []), args.status
            ))
            print("card updated")
        elif args.command == "opencode-decide":
            asyncio.run(
                opencode_decide(
                    args.session, args.perm, args.decision, args.opencode_url
                )
            )
            print(f"decided: {args.decision}")
        elif args.command == "opencode-reply-result":
            asyncio.run(
                opencode_reply_result(paths, args.perm, args.decision)
            )
            print(f"card updated: {args.decision}")
        elif args.command == "native-hook":
            if args.hook_name == "stop":
                native_stop_hook(paths, sys.stdin.read())
        elif args.command == "decide":
            ensure_service(paths)
            status = asyncio.run(
                decide_approval(paths, args.decision, args.approval_id)
            )
            message = approval_decision_message(
                status, args.decision, args.approval_id, args.quiet
            )
            if message:
                print(message)
        elif args.command == "status":
            running = _socket_ready(paths.proxy_socket)
            pid = paths.pid_file.read_text().strip() if paths.pid_file.exists() else "-"
            print(f"status: {'running' if running else 'stopped'}")
            print(f"pid: {pid}")
            print(f"socket: {paths.proxy_socket}")
        elif args.command == "doctor":
            codex_binary = resolve_codex_binary()
            codex_version, cc_version = check_versions(
                require_cc=args.strict,
                codex_binary=codex_binary,
                require_codex=args.strict,
            )
            print(
                f"Codex CLI: {codex_version if args.strict else 'not checked'}"
                f"{' (supported)' if args.strict else ''}"
            )
            print(
                f"cc-connect: {cc_version + ' (supported)' if cc_version else 'not checked'}"
            )
            opencode = shutil.which("opencode")
            opencode_version = "not found"
            if opencode:
                try:
                    opencode_version = _run_version([opencode, "--version"]).strip().splitlines()[0]
                except (OSError, RuntimeError):
                    opencode_version = f"found at {opencode}, version unavailable"
            print(f"OpenCode: {opencode_version}")
            print(f"state: {paths.state_dir}")
            print(f"runtime: {paths.runtime_dir}")
            print(f"service: {'running' if _socket_ready(paths.proxy_socket) else 'stopped'}")
            if args.strict:
                config = NotifierConfig.load(paths.config_file)
                missing = [
                    name for name in ("default", "acp")
                    if name not in config.notification_routes
                ]
                if missing:
                    raise ValueError(
                        "missing notification routes: " + ", ".join(missing)
                    )
                from .multi_backend import _load_chat_routes
                chat_routes = _load_chat_routes(paths.config_file)
                if "opencode" not in chat_routes.values() or "silent" not in chat_routes.values():
                    raise ValueError(
                        "chat_routes must contain an opencode interactive chat and a silent notification chat"
                    )
                if not opencode:
                    raise FileNotFoundError("OpenCode is required by doctor --strict")
                print("routes: default, acp, chat_routes (configured)")
        elif args.command == "init-config":
            initialized = initialize_user_config(paths)
            state = "created" if initialized.created else "exists"
            print(f"{state}: {initialized.path}")
        elif args.command == "setup":
            initialized = initialize_user_config(paths)
            if initialized.created:
                print(f"created: {initialized.path}")
            run_setup(
                paths,
                project=args.project,
                interactive_chat=args.interactive_chat_id,
                notification_chat=args.notification_chat_id,
                non_interactive=args.non_interactive,
            )
        elif args.command == "logs":
            journalctl = shutil.which("journalctl")
            if journalctl:
                command = [journalctl, "--user", "-u", "agent-notifier.service", "-n", "100"]
                if not args.no_follow:
                    command.append("-f")
                os.execvp(journalctl, command)
            log = paths.log_dir / "service.log"
            command = ["tail", "-n", "100"]
            if not args.no_follow:
                command.append("-f")
            command.append(str(log))
            os.execvp("tail", command)
        elif args.command == "configure-cc":
            backup = configure_cc(paths, args.project, args.config)
            print(f"configured: {args.config}")
            print(f"backup: {backup}")
        elif args.command == "restore-cc":
            backup = restore_cc(paths, args.config)
            print(f"restored from: {backup}")
        elif args.command == "configure-hooks":
            backup = configure_hooks(paths, args.hooks)
            if backup:
                print(f"configured: {args.hooks}")
                print(f"backup: {backup}")
            else:
                print("no ungated Stop/PermissionRequest hooks found")
        elif args.command == "configure-native-hooks":
            backup = configure_native_hooks(paths, args.hooks)
            if backup:
                print(f"configured native Stop hook: {args.hooks}")
                print(f"backup: {backup}")
            else:
                print(f"native Stop hook already configured: {args.hooks}")
        elif args.command == "restore-hooks":
            backup = restore_hooks(paths, args.hooks)
            print(f"restored from: {backup}")
        elif args.command == "hook-gate":
            if os.environ.get("AGENT_NOTIFIER_MANAGED") == "1":
                return
            command = decode_command(args.encoded)
            raise SystemExit(subprocess.call(["bash", "-lc", command]))
    except (CompatibilityError, FileNotFoundError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
