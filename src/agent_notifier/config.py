"""Filesystem locations and runtime defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    config_dir: Path
    state_dir: Path
    runtime_dir: Path
    log_dir: Path
    proxy_socket: Path
    upstream_socket: Path
    state_db: Path
    pid_file: Path
    lock_file: Path

    @classmethod
    def from_environment(cls) -> "Paths":
        home = Path(os.environ.get("HOME", str(Path.home())))
        config_root = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        state_root = Path(os.environ.get("XDG_STATE_HOME", home / ".local/state"))
        runtime_root = Path(
            os.environ.get("XDG_RUNTIME_DIR", f"/tmp/agent-notifier-{os.getuid()}")
        )
        config_dir = config_root / "agent-notifier"
        state_dir = state_root / "agent-notifier"
        runtime_dir = runtime_root / "agent-notifier"
        return cls(
            config_dir=config_dir,
            state_dir=state_dir,
            runtime_dir=runtime_dir,
            log_dir=state_dir / "logs",
            proxy_socket=runtime_dir / "proxy.sock",
            upstream_socket=runtime_dir / "codex-app-server.sock",
            state_db=state_dir / "state.sqlite3",
            pid_file=runtime_dir / "service.pid",
            lock_file=runtime_dir / "service.lock",
        )

    def ensure_directories(self) -> None:
        for directory in (self.config_dir, self.state_dir, self.runtime_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
