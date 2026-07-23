"""Reversible gates for notification hooks superseded by the managed service."""

from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path


GATED_EVENTS = {"Stop", "PermissionRequest"}
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


def default_hooks_path() -> Path:
    return Path.home() / ".codex/hooks.json"
