"""Native Codex lifecycle hook formatting and delivery helpers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

MAX_SUMMARY_CHARS = 280
MAX_RESULT_CHARS = 4000


def _first(payload: dict, *keys: str, default: str = "") -> str:
    for key in keys:
        value = payload.get(key)
        if value is not None and value != "":
            return str(value)
    return default


def compact(value: object, limit: int = MAX_SUMMARY_CHARS) -> str:
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def format_result(value: object, limit: int = MAX_RESULT_CHARS) -> str:
    """Preserve terminal line breaks and Markdown syntax for Feishu cards."""
    if isinstance(value, list):
        value = "\n".join(str(item) for item in value)
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def build_completion_message(payload: dict) -> tuple[str, str] | None:
    event_type = _first(payload, "type", "event_type", "event-type")
    hook_event_name = _first(payload, "hook_event_name", "hookEventName")
    if event_type != "agent-turn-complete" and hook_event_name != "Stop":
        return None

    thread_id = _first(
        payload,
        "thread-id",
        "thread_id",
        "threadId",
        "session_id",
        "sessionId",
        default=os.environ.get("CODEX_THREAD_ID", "unknown"),
    )
    cwd = _first(payload, "cwd", default=os.getcwd())
    project = Path(cwd).name or cwd
    task = compact(
        payload.get("input-messages")
        or payload.get("input_messages")
        or payload.get("last-user-message")
        or payload.get("last_user_message")
    )
    result = format_result(
        payload.get("last-assistant-message")
        or payload.get("last_assistant_message")
        or payload.get("message")
    )

    lines = [
        "Codex 回合已完成",
        f"**会话**：{project} | {thread_id[:8]}",
        f"**目录**：{cwd}",
    ]
    if task:
        lines.append(f"任务：{task}")
    if result:
        lines.append("**结果**：\n\n" + result)
    turn_id = _first(payload, "turn-id", "turn_id", "turnId", default="unknown")
    return "\n".join(lines), f"{thread_id}:{turn_id}"


def claim(dedupe_dir: Path, dedupe_key: str) -> bool:
    dedupe_dir.mkdir(parents=True, exist_ok=True)
    marker = dedupe_dir / hashlib.sha256(dedupe_key.encode()).hexdigest()
    try:
        marker.mkdir()
    except FileExistsError:
        return False
    return True
