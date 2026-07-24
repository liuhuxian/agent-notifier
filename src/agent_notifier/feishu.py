"""Feishu interactive approval cards."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiohttp import ClientSession

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


DEFAULT_CC_CONFIG = Path.home() / ".cc-connect" / "config.toml"


def load_feishu_settings(
    project_name: str, config_path: Path = DEFAULT_CC_CONFIG
) -> dict[str, str]:
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)
    matches = []
    for project in config.get("projects", []):
        if project.get("name") != project_name:
            continue
        for platform in project.get("platforms", []):
            if platform.get("type") == "feishu":
                matches.append((project, platform.get("options", {})))
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one Feishu platform for project {project_name!r}, "
            f"found {len(matches)}"
        )
    project, options = matches[0]
    settings = {
        "app_id": str(options.get("app_id", "")),
        "app_secret": str(options.get("app_secret", "")),
        "domain": str(options.get("domain", "https://open.feishu.cn")).rstrip("/"),
    }
    missing = [key for key in ("app_id", "app_secret") if not settings[key]]
    if missing:
        raise RuntimeError(f"missing Feishu settings: {', '.join(missing)}")
    return settings


def build_approval_card(
    approval_id: str,
    reason: str,
    operation: str,
    session_key: str,
    session_label: str,
    thread_id: str,
    cwd: str,
) -> dict[str, Any]:
    def button(label: str, kind: str, command: str) -> dict[str, Any]:
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": kind,
            "value": {
                "action": f"cmd:{command} {approval_id}",
                "session_key": session_key,
            },
        }

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": "Codex 权限审批"},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"会话：{session_label} | {thread_id[:8]}\n"
                    f"目录：`{cwd}`\n"
                    f"**请求 ID**：`{approval_id}`\n"
                    f"**原因**：{reason}\n"
                    f"**操作**：`{operation}`"
                ),
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    button("允许", "primary", "/codex-approve"),
                    button("拒绝", "danger", "/codex-deny"),
                ],
            },
        ],
    }


def build_approval_result_card(
    approval_id: str,
    decision: str,
    status: str,
    session_label: str,
    thread_id: str,
    cwd: str,
) -> dict[str, Any]:
    if status == "already_resolved":
        template = "orange"
        title = "审批已经处理"
        detail = "该权限请求已在其他终端处理，本次操作未改变审批结果。"
    elif decision == "allow":
        template = "green"
        title = "已允许 Codex 权限请求"
        detail = "Codex 已继续执行本次操作。"
    else:
        template = "red"
        title = "已拒绝 Codex 权限请求"
        detail = "Codex 已取消本次操作。"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"会话：{session_label} | {thread_id[:8]}\n"
                    f"目录：`{cwd}`\n"
                    f"**请求 ID**：`{approval_id}`\n"
                    f"{detail}"
                ),
            }
        ],
    }


async def _post_json(
    session: ClientSession,
    url: str,
    payload: dict[str, Any],
    token: str | None = None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with session.post(url, json=payload, headers=headers, timeout=10) as response:
        result = await response.json()
        if response.status >= 400 or result.get("code", 0) != 0:
            raise RuntimeError(
                f"Feishu API failed: http={response.status} "
                f"code={result.get('code')} msg={result.get('msg')}"
            )
        return result


async def _delete_json(
    session: ClientSession,
    url: str,
    token: str,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    async with session.delete(url, headers=headers, timeout=10) as response:
        result = await response.json()
        if response.status >= 400 or result.get("code", 0) != 0:
            raise RuntimeError(
                f"Feishu API failed: http={response.status} "
                f"code={result.get('code')} msg={result.get('msg')}"
            )
        return result


async def _tenant_access_token(
    session: ClientSession,
    settings: dict[str, str],
) -> str:
    auth = await _post_json(
        session,
        f"{settings['domain']}/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": settings["app_id"], "app_secret": settings["app_secret"]},
    )
    token = auth.get("tenant_access_token")
    if not token:
        raise RuntimeError("Feishu API did not return tenant_access_token")
    return str(token)


async def send_progress_message_with_onit(
    project: str,
    receive_id: str,
    text: str,
    config_path: Path = DEFAULT_CC_CONFIG,
) -> tuple[str, str]:
    settings = load_feishu_settings(project, config_path)
    async with ClientSession() as session:
        token = await _tenant_access_token(session, settings)
        sent = await _post_json(
            session,
            f"{settings['domain']}/open-apis/im/v1/messages"
            "?receive_id_type=open_id",
            {
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            token,
        )
        message_id = str(sent.get("data", {}).get("message_id", ""))
        if not message_id:
            raise RuntimeError("Feishu API did not return message_id")
        reacted = await _post_json(
            session,
            f"{settings['domain']}/open-apis/im/v1/messages/"
            f"{message_id}/reactions",
            {"reaction_type": {"emoji_type": "OnIt"}},
            token,
        )
        reaction_id = str(reacted.get("data", {}).get("reaction_id", ""))
        if not reaction_id:
            raise RuntimeError("Feishu API did not return reaction_id")
        return message_id, reaction_id


async def send_text_message(
    project: str,
    receive_id: str,
    receive_id_type: str,
    text: str,
    config_path: Path = DEFAULT_CC_CONFIG,
) -> str:
    settings = load_feishu_settings(project, config_path)
    async with ClientSession() as session:
        token = await _tenant_access_token(session, settings)
        sent = await _post_json(
            session,
            f"{settings['domain']}/open-apis/im/v1/messages"
            f"?receive_id_type={receive_id_type}",
            {
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            token,
        )
        message_id = str(sent.get("data", {}).get("message_id", ""))
        if not message_id:
            raise RuntimeError("Feishu API did not return message_id")
        return message_id


async def send_markdown_message(
    project: str,
    receive_id: str,
    receive_id_type: str,
    text: str,
    config_path: Path = DEFAULT_CC_CONFIG,
) -> str:
    """Send Markdown through a text-only interactive card."""
    settings = load_feishu_settings(project, config_path)
    async with ClientSession() as session:
        token = await _tenant_access_token(session, settings)
        card = {
            "config": {"wide_screen_mode": True},
            "elements": [{"tag": "markdown", "content": text}],
        }
        sent = await _post_json(
            session,
            f"{settings['domain']}/open-apis/im/v1/messages"
            f"?receive_id_type={receive_id_type}",
            {
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            token,
        )
        message_id = str(sent.get("data", {}).get("message_id", ""))
        if not message_id:
            raise RuntimeError("Feishu API did not return message_id")
        return message_id


async def remove_message_reaction(
    project: str,
    message_id: str,
    reaction_id: str,
    config_path: Path = DEFAULT_CC_CONFIG,
) -> None:
    settings = load_feishu_settings(project, config_path)
    async with ClientSession() as session:
        token = await _tenant_access_token(session, settings)
        await _delete_json(
            session,
            f"{settings['domain']}/open-apis/im/v1/messages/"
            f"{message_id}/reactions/{reaction_id}",
            token,
        )


async def send_approval_card(
    project: str,
    receive_id: str,
    session_key: str,
    approval_id: str,
    reason: str,
    operation: str,
    session_label: str,
    thread_id: str,
    cwd: str,
    receive_id_type: str = "open_id",
    config_path: Path = DEFAULT_CC_CONFIG,
) -> str:
    settings = load_feishu_settings(project, config_path)
    async with ClientSession() as session:
        auth = await _post_json(
            session,
            f"{settings['domain']}/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": settings["app_id"], "app_secret": settings["app_secret"]},
        )
        token = auth.get("tenant_access_token")
        if not token:
            raise RuntimeError("Feishu API did not return tenant_access_token")
        card = build_approval_card(
            approval_id,
            reason,
            operation,
            session_key,
            session_label,
            thread_id,
            cwd,
        )
        result = await _post_json(
            session,
            f"{settings['domain']}/open-apis/im/v1/messages"
            f"?receive_id_type={receive_id_type}",
            {
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            token,
        )
        return str(result.get("data", {}).get("message_id", ""))


async def reply_approval_result_card(
    project: str,
    message_id: str,
    approval_id: str,
    decision: str,
    status: str,
    session_label: str,
    thread_id: str,
    cwd: str,
    config_path: Path = DEFAULT_CC_CONFIG,
) -> str:
    settings = load_feishu_settings(project, config_path)
    async with ClientSession() as session:
        auth = await _post_json(
            session,
            f"{settings['domain']}/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": settings["app_id"], "app_secret": settings["app_secret"]},
        )
        token = auth.get("tenant_access_token")
        if not token:
            raise RuntimeError("Feishu API did not return tenant_access_token")
        card = build_approval_result_card(
            approval_id,
            decision,
            status,
            session_label,
            thread_id,
            cwd,
        )
        result = await _post_json(
            session,
            f"{settings['domain']}/open-apis/im/v1/messages/{message_id}/reply",
            {
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            token,
        )
        return str(result.get("data", {}).get("message_id", ""))
