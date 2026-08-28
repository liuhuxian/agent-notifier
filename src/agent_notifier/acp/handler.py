"""ACP method translation independent of the stdio transport."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import UUID, uuid4

from agent_notifier.registry import SessionRegistry


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
        registry: SessionRegistry | None = None,
    ):
        self.backend = backend
        self.cwd = cwd
        self.project = project
        self.external_key = external_key
        self.emit = emit
        self.registry = registry
        self.session_id: str | None = None
        self._thread_id: str | None = None

    def _new_acp_session_id(self) -> str:
        return str(uuid4())

    def _remember(self, acp_session_id: str, thread_id: str, cwd: str) -> None:
        self.session_id = acp_session_id
        self._thread_id = thread_id
        if self.registry is not None:
            self.registry.bind_acp_session(
                acp_session_id, thread_id, self.project, self.external_key, cwd
            )

    def _lookup_thread(self, acp_session_id: str) -> str | None:
        if acp_session_id == self.session_id and self._thread_id:
            return self._thread_id
        if self.registry is not None:
            mapping = self.registry.get_acp_session(acp_session_id)
            if mapping is not None:
                self.session_id = acp_session_id
                self._thread_id = mapping.thread_id
                return mapping.thread_id
        return None

    @staticmethod
    def _looks_like_uuid(value: str) -> bool:
        try:
            UUID(value.removeprefix("urn:uuid:"))
        except (ValueError, AttributeError):
            return False
        return True

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
            thread_id = await self.backend.start_thread(
                cwd, self.project, self.external_key
            )
            acp_session_id = self._new_acp_session_id()
            self._remember(acp_session_id, thread_id, cwd)
            return {"sessionId": acp_session_id}
        if method == "session/load":
            acp_session_id = params.get("sessionId")
            if not acp_session_id:
                raise ACPMethodError(-32602, "sessionId is required")
            cwd = params.get("cwd") or self.cwd
            thread_id = self._lookup_thread(acp_session_id)
            if thread_id is None:
                # Compatibility path for a backend-native ID or a Codex UUID
                # received from an installation predating this mapping.
                thread_id = await self.backend.resume_thread(
                    acp_session_id, cwd, self.project, self.external_key
                )
                if self._looks_like_uuid(acp_session_id):
                    mapped_id = acp_session_id
                else:
                    mapped_id = self._new_acp_session_id()
                self._remember(mapped_id, thread_id, cwd)
            else:
                # Re-register the native thread with a fresh dispatcher after
                # an ACP process restart.  Backends use this hook to rebuild
                # their in-memory subscriber/route map; OpenCode does not
                # create a second native session when the registry is present.
                thread_id = await self.backend.resume_thread(
                    thread_id, cwd, self.project, self.external_key
                )
                self._remember(acp_session_id, thread_id, cwd)
            return {"sessionId": self.session_id}
        if method == "session/prompt":
            cc_session_id = params.get("sessionId") or self.session_id
            if not cc_session_id:
                raise ACPMethodError(-32602, "session is not initialized")
            transport_thread_id = self._lookup_thread(cc_session_id)
            target_thread_id = self.backend.resolve_active_thread(
                self.project, self.external_key,
                transport_thread_id or cc_session_id,
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
                transport_thread_id = self._lookup_thread(cc_session_id)
                target_thread_id = self.backend.resolve_active_thread(
                    self.project, self.external_key,
                    transport_thread_id or cc_session_id,
                )
                await self.backend.cancel(target_thread_id)
            return {}
        raise ACPMethodError(-32601, f"method not implemented: {method}")
