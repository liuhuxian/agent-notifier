"""Opencode backend implementing the AgentBackend protocol."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from agent_notifier.config import ProgressConfig
from agent_notifier.registry import SessionRegistry

from .client import OpencodeClient


class OpencodeBackend:
    def __init__(
        self,
        client: OpencodeClient,
        registry: SessionRegistry,
        request_permission: Callable[[dict], Awaitable[dict]],
        progress: ProgressConfig | None = None,
    ):
        self._client = client
        self.registry = registry
        self._request_permission = request_permission
        self.progress = progress or ProgressConfig()
        self._session_ids: dict[str, str] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._dispatcher: asyncio.Task | None = None

    def _ensure_dispatcher(self) -> None:
        if self._dispatcher is None:
            self._dispatcher = asyncio.create_task(self._dispatch_events())

    async def _dispatch_events(self) -> None:
        while True:
            event = await self._client.next_event()
            event_type = event.get("type")
            if event_type == "_error":
                for queues in list(self._subscribers.values()):
                    for queue in list(queues):
                        await queue.put(event)
                return
            props = event.get("properties") or {}
            session_id = props.get("sessionID")
            if session_id:
                for queue in list(self._subscribers.get(session_id, [])):
                    await queue.put(event)

    def _register_transport_session(
        self,
        project: str,
        external_key: str,
        session_id: str,
        cwd: str,
    ) -> None:
        active = self.registry.get("cc_connect", project, external_key)
        if active is None:
            self.registry.bind(
                "cc_connect", project, external_key, session_id, cwd
            )
        else:
            self.registry.subscribe(
                "cc_connect", project, external_key, session_id, cwd
            )

    async def start_thread(self, cwd: str, project: str, external_key: str) -> str:
        result = await self._client.create_session()
        session_id = result["id"]
        self._register_transport_session(project, external_key, session_id, cwd)
        return session_id

    async def resume_thread(
        self, session_id: str, cwd: str, project: str, external_key: str
    ) -> str:
        mapping = self.registry.get("cc_connect", project, external_key)
        if mapping is not None:
            self._register_transport_session(
                project, external_key, mapping.thread_id, cwd
            )
            return mapping.thread_id
        sessions = await self._client.get_sessions()
        target: str | None = None
        for s in sessions:
            sid = s.get("id", "")
            if sid == session_id or sid.startswith(session_id):
                target = sid
                break
        if not target:
            raise ValueError(f"no opencode session matches {session_id!r}")
        self._register_transport_session(project, external_key, target, cwd)
        return target

    def resolve_active_thread(
        self, project: str, external_key: str, fallback: str
    ) -> str:
        mapping = self.registry.get("cc_connect", project, external_key)
        return mapping.thread_id if mapping else fallback

    async def prompt(
        self,
        session_id: str,
        text: str,
        origin: str,
        emit: Callable[[dict], Awaitable[None]],
    ) -> dict:
        self._ensure_dispatcher()
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[session_id].append(queue)
        final_text: str | None = None
        try:
            await self._client.send_prompt(session_id, text)
            if self.progress.progress_card:
                await emit({"kind": "status", "text": "正在思考"})

            while True:
                event = await queue.get()
                event_type = event.get("type")
                if event_type == "_error":
                    raise RuntimeError(
                        event.get("error", "opencode SSE connection lost")
                    )
                props = event.get("properties") or {}

                if event_type == "message.part.delta":
                    part = props.get("part") or props.get("info") or {}
                    if part.get("type") == "text" and not part.get("synthetic"):
                        delta = part.get("text", "")
                        if delta:
                            if self.progress.stream_preview:
                                await emit({"kind": "text", "text": delta})

                elif event_type == "message.updated":
                    info = props.get("info") or {}
                    if info.get("role") == "assistant":
                        parts = info.get("parts", [])
                        chunks = []
                        for p in parts:
                            if p.get("type") == "text" and not p.get("synthetic"):
                                chunks.append(p.get("text", ""))
                        if chunks:
                            final_text = "".join(chunks).strip()

                elif event_type == "permission.asked":
                    perm_id = props.get("id")
                    if perm_id:
                        await self._forward_permission(props, session_id, emit)

                elif event_type == "session.idle":
                    if final_text:
                        await emit({"kind": "text", "text": final_text})
                    return {"stopReason": "end_turn"}

                elif event_type == "session.error":
                    error = props.get("error") or props.get("message") or "unknown error"
                    raise RuntimeError(error)

                elif event_type in ("session.status", "session.updated"):
                    status = info.get("type") if "info" in props else props.get("type")
                    if status == "idle" and not any(
                        e.get("type") == "session.idle" for e in [event]
                    ):
                        pass
        finally:
            self._subscribers[session_id].remove(queue)
            if not self._subscribers[session_id]:
                del self._subscribers[session_id]

        if final_text:
            return {"stopReason": "end_turn"}
        return {"stopReason": "cancelled"}

    async def _forward_permission(
        self,
        props: dict,
        session_id: str,
        emit: Callable[[dict], Awaitable[None]],
    ) -> None:
        perm_id = props.get("id")
        perm_type = props.get("permission", "unknown")
        meta = props.get("metadata") or {}
        filepath = meta.get("filepath", "")
        tool_call = {
            "toolCallId": perm_id,
            "title": f"opencode: {perm_type}",
            "kind": "permission",
            "rawInput": {"permission": perm_type, "filepath": filepath},
        }
        if self.progress.progress_card:
            await emit({
                "kind": "tool_start",
                "tool_call_id": perm_id,
                "title": tool_call["title"],
                "tool_kind": "permission",
                "raw_input": tool_call["rawInput"],
            })
        try:
            result = await self._request_permission({
                "sessionId": session_id,
                "toolCall": tool_call,
                "options": [
                    {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "deny_once", "name": "Deny", "kind": "reject_once"},
                ],
            })
            outcome = result.get("outcome") or {}
            allowed = (
                outcome.get("outcome") == "selected"
                and outcome.get("optionId") == "allow_once"
            )
            response = "once" if allowed else "reject"
            if self.progress.progress_card:
                await emit({
                    "kind": "tool_complete",
                    "tool_call_id": perm_id,
                    "status": "completed" if allowed else "failed",
                })
        except Exception:
            response = "reject"
            await emit({
                "kind": "tool_complete",
                "tool_call_id": perm_id,
                "status": "failed",
            })
        await self._client.reply_permission(session_id, perm_id, response)

    async def cancel(self, session_id: str) -> None:
        async with self._client._session.post(
            f"{self._client.base_url}/session/{session_id}/abort"
        ) as resp:
            if resp.status >= 400:
                data = await resp.json()
                raise RuntimeError(
                    f"cancel session failed: {data.get('error', resp.status)}"
                )

    async def close(self) -> None:
        if self._dispatcher:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
            self._dispatcher = None
        await self._client.close()