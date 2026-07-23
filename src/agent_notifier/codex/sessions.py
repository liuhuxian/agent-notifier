"""Codex rollout-session discovery."""

from __future__ import annotations

import os
import re
from pathlib import Path


_THREAD_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\.jsonl$"
)


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def discover_rollout_thread_ids(home: Path | None = None) -> set[str]:
    sessions_dir = (home or codex_home()) / "sessions"
    if not sessions_dir.is_dir():
        return set()
    thread_ids = set()
    for path in sessions_dir.rglob("rollout-*.jsonl"):
        match = _THREAD_ID_RE.search(path.name)
        if match:
            thread_ids.add(match.group(1))
    return thread_ids
