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
            "width": "fill",
            "behaviors": [{
                "type": "callback",
                "value": {
                    "action": f"cmd:{command} {approval_id}",
                    "session_key": session_key,
                },
            }],
        }

    return build_card_v2(
        "orange",
        "Codex 权限审批",
        [
            {
                "tag": "markdown",
                "content": (
                    f"**会话**：{session_label} | {thread_id[:8]}\n"
                    f"**目录**：`{cwd}`\n"
                    f"**请求 ID**：`{approval_id}`\n"
                    f"**原因**：{reason}\n"
                    f"**操作**：`{operation}`"
                ),
            },
            {"tag": "hr"},
            build_button_set([
                button("允许", "primary", "/codex-approve"),
                button("拒绝", "danger", "/codex-deny"),
            ]),
        ],
    )


def build_approval_result_card(
    approval_id: str,
    decision: str,
    status: str,
    session_label: str,
    thread_id: str,
    cwd: str,
    reason: str | None = None,
    operation: str | None = None,
) -> dict[str, Any]:
    if status == "expired":
        template = "grey"
        title = "已失效：Codex 权限请求"
        detail = "该审批因 Agent Notifier 重启已失效，请重新触发。"
    elif status == "already_resolved":
        template = "grey"
        title = "已处理：Codex 权限请求"
        detail = "该权限请求已在其他终端处理，本次操作未改变审批结果。"
    elif decision == "allow":
        template = "green"
        title = "已允许：Codex 权限请求"
        detail = "Codex 已继续执行本次操作。"
    else:
        template = "red"
        title = "已拒绝：Codex 权限请求"
        detail = "Codex 已取消本次操作。"
    content = (
        f"**会话**：{session_label} | {thread_id[:8]}\n"
        f"**目录**：`{cwd}`\n"
        f"**请求 ID**：`{approval_id}`\n"
    )
    if reason is not None and operation is not None:
        content += f"**原因**：{reason}\n**操作**：`{operation}`"
        if status == "expired":
            content += f"\n\n{detail}"
    else:
        content += detail
    return build_card_v2(
        template,
        title,
        [
            {
                "tag": "markdown",
                "content": content,
            }
        ],
    )


def build_card_v2(
    template: str, title: str, elements: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build a Feishu interactive Card V2 with the supplied body elements."""
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title[:40]},
        },
        "body": {"elements": elements},
    }


def build_button_set(buttons: list[dict[str, Any]]) -> dict[str, Any]:
    """Place V2 buttons in one horizontal row."""
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [button],
            }
            for button in buttons
        ],
    }


def build_opencode_approval_card(
    perm_id: str,
    perm_type: str,
    filepath: str,
    pattern: str = "",
    session_key: str = "",
    options: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Render an OpenCode permission request with its available choices."""
    content_lines = [f"**类型**: {perm_type}"]
    if pattern:
        content_lines.append(f"**操作**: {pattern}")
    if filepath:
        content_lines.append(f"**路径**: {filepath}")
    content_lines.append(f"**ID**: {perm_id[-8:] if perm_id else 'unknown'}")
    option_defs = options or [
        {"option_id": "once", "label": "允许一次"},
        {"option_id": "reject", "label": "拒绝"},
    ]
    buttons = []
    for option in option_defs:
        option_id = str(option.get("option_id", "")).strip()
        label = str(option.get("label", option_id)).strip() or option_id
        if not option_id:
            continue
        if option_id == "once":
            action = f"cmd:/opencode-approve {perm_id}"
        elif option_id == "reject":
            action = f"cmd:/opencode-deny {perm_id}"
        else:
            action = f"cmd:/opencode-select {perm_id} {option_id}"
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": label[:20]},
            "type": "danger" if option_id == "reject" else "primary",
            "width": "fill",
            "behaviors": [{
                "type": "callback",
                "value": {
                    "action": action,
                    **({"session_key": session_key} if session_key else {}),
                },
            }],
        })
    if not buttons:
        raise ValueError("OpenCode permission request has no selectable options")
    return build_card_v2(
        "orange",
        "OpenCode 权限请求",
        [
            {"tag": "markdown", "content": "\n".join(content_lines)},
            {"tag": "hr"},
            build_button_set(buttons),
        ],
    )


def build_opencode_question_card(
    request_id: str,
    session_id: str,
    questions: list[dict[str, Any]],
    session_key: str = "",
) -> dict[str, Any]:
    """Render an OpenCode question request with one option row per question."""
    elements: list[dict[str, Any]] = []
    for question_index, question in enumerate(questions):
        header = str(question.get("header", "问题")).strip() or "问题"
        text = str(question.get("question", "")).strip()
        content = f"**{header}**"
        if text:
            content += f"\n{text}"
        if question.get("custom", True):
            content += "\n（当前支持选项选择；自定义文字请在 OpenCode 终端输入）"
        elements.append({"tag": "markdown", "content": content})
        buttons = []
        for option_index, option in enumerate(question.get("options", [])):
            label = str(option.get("label", option.get("value", ""))).strip()
            if not label:
                continue
            action = f"cmd:/opencode-question-select {request_id} {question_index} {option_index}"
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": label[:20]},
                "type": "primary",
                "width": "fill",
                "behaviors": [{
                    "type": "callback",
                    "value": {
                        "action": action,
                        **({"session_key": session_key} if session_key else {}),
                    },
                }],
            })
        if not buttons:
            raise ValueError(f"OpenCode question {question_index} has no options")
        elements.append(build_button_set(buttons))
        if question_index != len(questions) - 1:
            elements.append({"tag": "hr"})
    elements.extend([
        {"tag": "hr"},
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "提交回答"},
            "type": "primary",
            "width": "fill",
            "behaviors": [{
                "type": "callback",
                "value": {
                    "action": f"cmd:/opencode-question-submit {request_id}",
                    **({"session_key": session_key} if session_key else {}),
                },
            }],
        },
    ])
    return build_card_v2(
        "orange",
        "OpenCode 问题选择",
        [{"tag": "markdown", "content": f"**会话**：{session_id[-8:]}\n**请求 ID**：{request_id[-8:]}"}, *elements],
    )


async def send_opencode_question_card(
    project: str,
    receive_id: str,
    receive_id_type: str,
    session_id: str,
    request_id: str,
    questions: list[dict[str, Any]],
    session_key: str = "",
    config_path: Path = DEFAULT_CC_CONFIG,
) -> str:
    settings = load_feishu_settings(project, config_path)
    async with ClientSession() as session:
        token = await _tenant_access_token(session, settings)
        card = build_opencode_question_card(
            request_id, session_id, questions, session_key
        )
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


def build_opencode_question_result_card(
    request_id: str,
    session_id: str,
    questions: list[dict[str, Any]],
    answers: list[list[str]],
    status: str = "answered",
) -> dict[str, Any]:
    if status == "terminal":
        template, title = "grey", "已处理：OpenCode 问题选择"
    elif status == "rejected":
        template, title = "grey", "已拒绝：OpenCode 问题选择"
    else:
        template, title = "green", "已回答：OpenCode 问题选择"
    elements = [{
        "tag": "markdown",
        "content": f"**会话**：{session_id[-8:]}\n**请求 ID**：{request_id[-8:]}",
    }]
    for index, question in enumerate(questions):
        header = str(question.get("header", "问题")).strip() or "问题"
        selected = answers[index] if index < len(answers) else []
        values = "、".join(str(value) for value in selected) or "未选择"
        elements.append({
            "tag": "markdown",
            "content": f"**{header}**\n{question.get('question', '')}\n**已选择**：{values}",
        })
    return build_card_v2(template, title, elements)


async def update_opencode_question_card(
    project: str,
    message_id: str,
    request_id: str,
    session_id: str,
    questions: list[dict[str, Any]],
    answers: list[list[str]],
    status: str = "answered",
    config_path: Path = DEFAULT_CC_CONFIG,
) -> str:
    settings = load_feishu_settings(project, config_path)
    async with ClientSession() as session:
        token = await _tenant_access_token(session, settings)
        card = build_opencode_question_result_card(
            request_id, session_id, questions, answers, status
        )
        sent = await _patch_json(
            session,
            f"{settings['domain']}/open-apis/im/v1/messages/{message_id}",
            {
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            token,
        )
        return str(sent.get("data", {}).get("message_id", message_id))


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


async def _patch_json(
    session: ClientSession,
    url: str,
    payload: dict[str, Any],
    token: str | None = None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with session.patch(url, json=payload, headers=headers, timeout=10) as response:
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
        card = build_markdown_card_v2("执行中", text)
        sent = await _post_json(
            session,
            f"{settings['domain']}/open-apis/im/v1/messages"
            "?receive_id_type=open_id",
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
        lines = text.splitlines()
        candidate = lines[0].strip() if lines else ""
        has_title = candidate.startswith((
            "Codex 回合已完成",
            "Codex 回合异常中断",
            "OpenCode 回合已完成",
            "OpenCode 回合异常中断",
            "OpenCode 权限请求",
            "OpenCode 错误",
            "Call-Agent 进度通知",
        ))
        title = candidate[:40] if has_title else "通知"
        body = (
            "\n".join(lines[1:]).lstrip("\n")
            if has_title
            else text
        )
        card = build_markdown_card_v2(title, body)
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


def build_markdown_card_v2(title: str, body: str) -> dict[str, Any]:
    """Build a blue Feishu Card V2 containing Markdown content."""
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title[:40]},
        },
        "body": {"elements": [{"tag": "markdown", "content": body}]},
    }


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
    reason: str | None = None,
    operation: str | None = None,
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
            reason,
            operation,
        )
        result = await _patch_json(
            session,
            f"{settings['domain']}/open-apis/im/v1/messages/{message_id}",
            {
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
            },
            token,
        )
        return str(result.get("data", {}).get("message_id", ""))


async def send_opencode_approval_card(
    project: str,
    receive_id: str,
    receive_id_type: str,
    session_id: str,
    perm_id: str,
    perm_type: str,
    filepath: str,
    pattern: str = "",
    session_key: str = "",
    options: list[dict[str, str]] | None = None,
    config_path: Path = DEFAULT_CC_CONFIG,
) -> str:
    settings = load_feishu_settings(project, config_path)
    async with ClientSession() as session:
        token = await _tenant_access_token(session, settings)
        card = build_opencode_approval_card(
            perm_id, perm_type, filepath, pattern, session_key, options
        )
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


async def update_opencode_approval_card(
    project: str,
    message_id: str,
    perm_type: str,
    filepath: str,
    decision: str,
    config_path: Path = DEFAULT_CC_CONFIG,
) -> str:
    settings = load_feishu_settings(project, config_path)
    async with ClientSession() as session:
        token = await _tenant_access_token(session, settings)
        if decision in {"once", "always"}:
            template, title, detail = "green", "已允许: OpenCode 权限请求", "已允许，OpenCode 继续执行。"
        elif decision == "reject":
            template, title, detail = "red", "已拒绝: OpenCode 权限请求", "已拒绝，OpenCode 取消操作。"
        else:
            template, title, detail = "grey", "已处理: OpenCode 权限请求", "该权限请求已处理。"
        card = build_card_v2(
            template,
            title,
            [
                {
                    "tag": "markdown",
                    "content": (
                        f"**类型**: {perm_type}\n"
                        f"**路径**: {filepath}\n\n"
                        f"{detail}"
                    ),
                },
            ],
        )
        sent = await _patch_json(
            session,
            f"{settings['domain']}/open-apis/im/v1/messages/{message_id}",
            {
                "content": json.dumps(card, ensure_ascii=False),
            },
            token,
        )
        return str(sent.get("data", {}).get("message_id", message_id))
