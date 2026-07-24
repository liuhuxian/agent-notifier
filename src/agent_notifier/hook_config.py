"""Reversible gates for notification hooks superseded by the managed service."""

from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path


GATED_EVENTS = {"Stop"}
GATE_MARKER = "agent-notifier hook-gate --encoded"


def encode_command(command: str) -> str:
    return base64.urlsafe_b64encode(command.encode()).decode()


def decode_command(encoded: str) -> str:
    return base64.urlsafe_b64decode(encoded.encode()).decode()


def gate_hooks(source: str, executable: str) -> tuple[str, int]:
    document = json.loads(source)
    hooks = document.get("hooks") or {}
    changed = 0
    for event in GATED_EVENTS:
        for matcher in hooks.get(event) or []:
            for handler in matcher.get("hooks") or []:
                command = handler.get("command")
                if not isinstance(command, str) or GATE_MARKER in command:
                    continue
                handler["command"] = (
                    f"{shlex.quote(executable)} hook-gate --encoded "
                    f"{shlex.quote(encode_command(command))}"
                )
                changed += 1
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n", changed


def native_stop_hook(source: str, executable: str) -> tuple[str, bool]:
    """Install the package-owned native Stop hook without touching approvals."""
    document = json.loads(source) if source.strip() else {"hooks": {}}
    hooks = document.setdefault("hooks", {})
    stop_entries = hooks.get("Stop") or []
    command = f"{shlex.quote(executable)} native-hook stop"
    managed_command = (
        f"{shlex.quote(executable)} hook-gate --encoded "
        f"{shlex.quote(encode_command(command))}"
    )
    desired = {
        "matcher": ".*",
        "hooks": [
            {
                "type": "command",
                "command": managed_command,
                "timeout": 15,
                "statusMessage": "发送飞书完成通知",
            }
        ],
    }

    kept = []
    replaced = False
    for entry in stop_entries:
        handlers = entry.get("hooks") or []
        owned = False
        for handler in handlers:
            old_command = handler.get("command", "")
            if GATE_MARKER in old_command:
                try:
                    old_command = decode_command(
                        old_command.split("--encoded", 1)[1].strip()
                    )
                except Exception:
                    pass
            if "feishu_notify.py" in old_command or "native-hook stop" in old_command:
                owned = True
        if owned:
            if not replaced:
                kept.append(desired)
                replaced = True
        else:
            kept.append(entry)
    if not replaced:
        kept.append(desired)
    hooks["Stop"] = kept
    updated = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    return updated, updated != source


def default_hooks_path() -> Path:
    return Path.home() / ".codex/hooks.json"
