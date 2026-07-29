"""Filesystem locations and runtime defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


@dataclass(frozen=True)
class ProgressConfig:
    onit: bool = True
    progress_card: bool = True
    stream_preview: bool = False
    stream_update_interval_ms: int = 2000
    moving_onit: bool = True
    notify_interruption: bool = True

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "ProgressConfig":
        config = cls(
            onit=bool(values.get("onit", True)),
            progress_card=bool(values.get("progress_card", True)),
            stream_preview=bool(values.get("stream_preview", False)),
            stream_update_interval_ms=int(
                values.get("stream_update_interval_ms", 2000)
            ),
            moving_onit=bool(values.get("moving_onit", True)),
            notify_interruption=bool(
                values.get("notify_interruption", True)
            ),
        )
        if config.stream_update_interval_ms < 250:
            raise ValueError("stream_update_interval_ms must be >= 250")
        return config


@dataclass(frozen=True)
class NotificationRoute:
    project: str
    receive_id: str
    receive_id_type: str = "chat_id"
    message_format: str = "text"
    session_key: str = ""

    @classmethod
    def from_mapping(
        cls, name: str, values: dict[str, Any]
    ) -> "NotificationRoute":
        project = str(values.get("project", "")).strip()
        receive_id = str(values.get("receive_id", "")).strip()
        receive_id_type = str(
            values.get("receive_id_type", "chat_id")
        ).strip()
        message_format = str(values.get("message_format", "text")).strip()
        session_key = str(values.get("session_key", "")).strip()
        if not project:
            raise ValueError(
                f"notification route {name!r} requires project"
            )
        if not receive_id:
            raise ValueError(
                f"notification route {name!r} requires receive_id"
            )
        if receive_id_type not in {"chat_id", "open_id"}:
            raise ValueError(
                f"notification route {name!r} has unsupported "
                f"receive_id_type: {receive_id_type}"
            )
        if message_format not in {"text", "markdown"}:
            raise ValueError(
                f"notification route {name!r} has unsupported "
                f"message_format: {message_format}"
            )
        return cls(project, receive_id, receive_id_type, message_format, session_key)


@dataclass(frozen=True)
class NotifierConfig:
    progress: ProgressConfig = ProgressConfig()
    # Agent-scoped fallback targets.  A persisted thread route takes
    # precedence; these names only resolve new/unbound sessions.
    agent_routes: dict[str, str] = field(
        default_factory=lambda: {"codex": "default", "opencode": "acp"}
    )
    notification_routes: dict[str, NotificationRoute] = field(
        default_factory=dict
    )

    @classmethod
    def load(cls, path: Path) -> "NotifierConfig":
        if not path.exists():
            return cls(notification_routes={})
        with path.open("rb") as stream:
            values = tomllib.load(stream)
        progress = values.get("progress") or {}
        if not isinstance(progress, dict):
            raise ValueError("[progress] must be a TOML table")
        raw_routes = values.get("notification_routes") or {}
        if not isinstance(raw_routes, dict):
            raise ValueError("[notification_routes] must be a TOML table")
        raw_agent_routes = values.get("agent_routes") or {}
        if not isinstance(raw_agent_routes, dict):
            raise ValueError("[agent_routes] must be a TOML table")
        agent_routes = {
            str(agent): str(route).strip()
            for agent, route in raw_agent_routes.items()
        }
        agent_routes = {
            "codex": agent_routes.get("codex", "default"),
            "opencode": agent_routes.get("opencode", "acp"),
        }
        routes = {}
        for name, route_values in raw_routes.items():
            if not isinstance(route_values, dict):
                raise ValueError(
                    f"[notification_routes.{name}] must be a TOML table"
                )
            routes[str(name)] = NotificationRoute.from_mapping(
                str(name), route_values
            )
        return cls(
            progress=ProgressConfig.from_mapping(progress),
            agent_routes=agent_routes,
            notification_routes=routes,
        )


@dataclass(frozen=True)
class ConfigInitialization:
    path: Path
    created: bool


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

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

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


def initialize_user_config(paths: Paths) -> ConfigInitialization:
    paths.ensure_directories()
    if paths.config_file.exists():
        return ConfigInitialization(paths.config_file, False)
    template = (
        resources.files("agent_notifier")
        .joinpath("resources/config.example.toml")
        .read_text(encoding="utf-8")
    )
    paths.config_file.write_text(template, encoding="utf-8")
    return ConfigInitialization(paths.config_file, True)
