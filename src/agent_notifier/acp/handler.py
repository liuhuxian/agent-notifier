"""ACP method translation independent of the stdio transport."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol


class AgentBackend(Protocol):
    async def start_thread(self, cwd: str, project: str, external_key: str) -> str: ...
    async def resume_thread(
        self, thread_id: str, cwd: str, project: str, external_key: str
    ) -> str: ...
    async def prompt(
        self,
        thread_id: str,
        text: str,
        origin: str,
        emit: Callable[[dict], Awaitable[None]],
    ) -> dict: ...
    def resolve_active_thread(
        self, project: str, external_key: str, fallback: str
    ) -> str: ...
    async def cancel(self, thread_id: str) -> None: ...


class ACPMethodError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class ACPHandler:
    def __init__(
        self,
        backend: AgentBackend,
        cwd: str,
        project: str,
        external_key: str,
        emit: Callable[[str, dict], Awaitable[None]],
    ):
        self.backend = backend
        self.cwd = cwd
        self.project = project
        self.external_key = external_key
        self.emit = emit
        self.session_id: str | None = None

    async def request(self, method: str, params: dict[str, Any]) -> dict:
        if method == "initialize":
            return {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True},
                "authMethods": [],
                "agentInfo": {"name": "agent-notifier", "version": "0.1.0"},
            }
        if method == "authenticate":
            return {}
        if method == "session/new":
            cwd = params.get("cwd") or self.cwd
            self.session_id = await self.backend.start_thread(
                cwd, self.project, self.external_key
            )
            return {"sessionId": self.session_id}
        if method == "session/load":
            session_id = params.get("sessionId")
            if not session_id:
                raise ACPMethodError(-32602, "sessionId is required")
            cwd = params.get("cwd") or self.cwd
            self.session_id = await self.backend.resume_thread(
                session_id, cwd, self.project, self.external_key
            )
            return {"sessionId": self.session_id}
        if method == "session/prompt":
            cc_session_id = params.get("sessionId") or self.session_id
            if not cc_session_id:
                raise ACPMethodError(-32602, "session is not initialized")
            target_thread_id = self.backend.resolve_active_thread(
                self.project, self.external_key, cc_session_id
            )
            text = "".join(
                block.get("text", "")
                for block in params.get("prompt", [])
                if block.get("type") == "text"
            )

            async def emit_backend(event: dict) -> None:
                if event.get("kind") == "text" and event.get("text"):
                    await self.emit(
                        "session/update",
                        {
                            "sessionId": cc_session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {
                                    "type": "text",
                                    "text": event["text"],
                                },
                            },
                        },
                    )
                elif event.get("kind") == "status" and event.get("text"):
                    await self.emit(
                        "session/update",
                        {
                            "sessionId": cc_session_id,
                            "update": {
                                "sessionUpdate": "agent_thought_chunk",
                                "content": {
                                    "type": "text",
                                    "text": event["text"],
                                },
                            },
                        },
                    )
                elif event.get("kind") == "tool_start":
                    await self.emit(
                        "session/update",
                        {
                            "sessionId": cc_session_id,
                            "update": {
                                "sessionUpdate": "tool_call",
                                "toolCallId": event["tool_call_id"],
                                "title": event["title"],
                                "kind": event.get("tool_kind", "other"),
                                "status": "in_progress",
                                "rawInput": event.get("raw_input", {}),
                            },
                        },
                    )
                elif event.get("kind") == "tool_complete":
                    await self.emit(
                        "session/update",
                        {
                            "sessionId": cc_session_id,
                            "update": {
                                "sessionUpdate": "tool_call_update",
                                "toolCallId": event["tool_call_id"],
                                "status": event.get("status", "completed"),
                            },
                        },
                    )

            return await self.backend.prompt(
                target_thread_id, text, "cc_connect", emit_backend
            )
        if method == "session/cancel":
            cc_session_id = params.get("sessionId") or self.session_id
            if cc_session_id:
                target_thread_id = self.backend.resolve_active_thread(
                    self.project, self.external_key, cc_session_id
                )
                await self.backend.cancel(target_thread_id)
            return {}
        raise ACPMethodError(-32601, f"method not implemented: {method}")
