"""Route ACP sessions to codex or opencode backend based on chat_id."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .acp.handler import AgentBackend
from .config import NotifierConfig, Paths
from .registry import SessionRegistry


def _extract_chat_id(external_key: str) -> str:
    parts = external_key.split(":", 2)
    return parts[1] if len(parts) >= 2 else ""


def _load_chat_routes(config_path: Path) -> dict[str, str]:
    if not config_path.exists():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    with config_path.open("rb") as f:
        data = tomllib.load(f)
    routes = data.get("chat_routes", {})
    if not isinstance(routes, dict):
        return {}
    return {str(k): str(v) for k, v in routes.items()}


class _SilentBackend:
    """Backend for notification-only chats — accepts prompts but returns empty."""

    def __init__(self, registry: SessionRegistry):
        self._registry = registry

    async def start_thread(self, cwd: str, project: str, external_key: str) -> str:
        sid = f"notify-{_extract_chat_id(external_key)[:12]}"
        self._registry.bind("cc_connect", project, external_key, sid, cwd)
        return sid

    async def resume_thread(
        self, thread_id: str, cwd: str, project: str, external_key: str
    ) -> str:
        return thread_id

    async def prompt(
        self,
        session_id: str,
        text: str,
        origin: str,
        emit: Callable[[dict], Awaitable[None]],
    ) -> dict:
        return {"stopReason": "end_turn"}

    def resolve_active_thread(
        self, project: str, external_key: str, fallback: str
    ) -> str:
        mapping = self._registry.get("cc_connect", project, external_key)
        return mapping.thread_id if mapping else fallback

    async def cancel(self, thread_id: str) -> None:
        pass

    async def close(self) -> None:
        pass


class MultiBackendDispatcher:
    def __init__(
        self,
        codex: AgentBackend,
        opencode: AgentBackend,
        registry: SessionRegistry,
        chat_routes: dict[str, str] | None = None,
        config_path: Path | None = None,
    ):
        self._codex = codex
        self._opencode = opencode
        self._registry = registry
        self._chat_routes = chat_routes or {}
        self._config_path = config_path
        self._silent = _SilentBackend(registry)
        self._thread_map: dict[str, AgentBackend] = {}

    @property
    def config_path(self) -> Path | None:
        return self._config_path

    def _resolve(self, external_key: str) -> tuple[str, AgentBackend]:
        chat_id = _extract_chat_id(external_key)
        agent_type = self._chat_routes.get(chat_id, "opencode")
        if agent_type == "codex":
            return "codex", self._codex
        if agent_type == "silent":
            return "silent", self._silent
        return "opencode", self._opencode

    async def start_thread(
        self, cwd: str, project: str, external_key: str
    ) -> str:
        agent_type, backend = self._resolve(external_key)
        thread_id = await backend.start_thread(cwd, project, external_key)
        self._thread_map[thread_id] = backend
        self._registry.set_active_agent(project, external_key, agent_type)
        return thread_id

    async def resume_thread(
        self, thread_id: str, cwd: str, project: str, external_key: str
    ) -> str:
        agent_type, backend = self._resolve(external_key)
        result = await backend.resume_thread(thread_id, cwd, project, external_key)
        self._thread_map[result] = backend
        self._registry.set_active_agent(project, external_key, agent_type)
        return result

    async def prompt(
        self,
        session_id: str,
        text: str,
        origin: str,
        emit: Callable[[dict], Awaitable[None]],
    ) -> dict:
        backend = self._thread_map.get(session_id, self._opencode)
        return await backend.prompt(session_id, text, origin, emit)

    def resolve_active_thread(
        self, project: str, external_key: str, fallback: str
    ) -> str:
        agent_type, _ = self._resolve(external_key)
        if agent_type == "codex":
            return self._codex.resolve_active_thread(project, external_key, fallback)
        if agent_type == "silent":
            return self._silent.resolve_active_thread(project, external_key, fallback)
        return self._opencode.resolve_active_thread(project, external_key, fallback)

    async def cancel(self, thread_id: str) -> None:
        backend = self._thread_map.get(thread_id)
        if backend:
            await backend.cancel(thread_id)
        else:
            for b in (self._codex, self._opencode, self._silent):
                try:
                    await b.cancel(thread_id)
                except Exception:
                    pass

    async def close(self) -> None:
        for backend in (self._codex, self._opencode, self._silent):
            try:
                await backend.close()
            except Exception:
                pass
