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


async def send_approval_card(
    project: str,
    receive_id: str,
    session_key: str,
    approval_id: str,
    reason: str,
    operation: str,
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
            approval_id, reason, operation, session_key
        )
        result = await _post_json(
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
        return str(result.get("data", {}).get("message_id", ""))
