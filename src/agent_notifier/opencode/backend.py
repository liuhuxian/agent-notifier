"""Opencode backend implementing the AgentBackend protocol."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
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
        tool_event_socket: Path | None = None,
    ):
        self._client = client
        self.registry = registry
        self._request_permission = request_permission
        self.progress = progress or ProgressConfig()
        self._session_ids: dict[str, str] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._dispatcher: asyncio.Task | None = None
        self._tool_event_socket = tool_event_socket
        self._tool_event_task: asyncio.Task | None = None
        self._active_emitters: dict[str, Callable[[dict], Awaitable[None]]] = {}
        self._tool_sock: socket.socket | None = None

    def _ensure_dispatcher(self) -> None:
        if self._dispatcher is None:
            self._dispatcher = asyncio.create_task(self._dispatch_events())

    def _ensure_tool_event_listener(self) -> None:
        if self._tool_event_socket is not None and self._tool_event_task is None:
            self._tool_event_task = asyncio.create_task(self._tool_event_loop())

    async def _tool_event_loop(self) -> None:
        path = self._tool_event_socket
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        sock: socket.socket | None = None
        try:
            if path.exists():
                path.unlink()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.setblocking(False)
            sock.bind(str(path))
            os.chmod(path, 0o600)
            self._tool_sock = sock
            loop = asyncio.get_running_loop()
            while True:
                raw = await loop.sock_recv(sock, 65535)
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                session_id = str(event.get("sessionID") or "")
                emit = self._active_emitters.get(session_id)
                if emit is None:
                    continue
                phase = event.get("phase")
                call_id = str(event.get("callID") or "")
                if not call_id or phase not in {"start", "complete"}:
                    continue
                if phase == "start":
                    await emit({
                        "kind": "tool_start",
                        "tool_call_id": call_id,
                        "title": str(event.get("title") or event.get("tool") or "opencode tool"),
                        "tool_kind": "other",
                        "raw_input": event.get("args") or {},
                    })
                else:
                    await emit({
                        "kind": "tool_complete",
                        "tool_call_id": call_id,
                        "status": "failed" if event.get("error") else "completed",
                    })
        except asyncio.CancelledError:
            raise
        finally:
            if self._tool_sock is not None:
                self._tool_sock.close()
                self._tool_sock = None
            elif sock is not None:
                sock.close()
            if path.exists():
                path.unlink()

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
        self._ensure_tool_event_listener()
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[session_id].append(queue)
        accumulated_text: list[str] = []
        part_types: dict[str, str] = {}
        assistant_msg_ids: set[str] = set()
        acp_flag = Path(f"/tmp/oc-acp-active-{session_id}")
        try:
            self._active_emitters[session_id] = emit
            acp_flag.write_text("1")
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

                if event_type == "message.updated":
                    info = props.get("info") or props.get("message") or {}
                    if info.get("role") == "assistant":
                        assistant_msg_ids.add(info.get("id", ""))

                elif event_type == "message.part.updated":
                    part = props.get("part") or {}
                    msg_id = part.get("messageID", "")
                    if part.get("type") == "text":
                        part_types[part["id"]] = "text"
                        if msg_id in assistant_msg_ids:
                            text_val = part.get("text", "")
                            if text_val and not accumulated_text:
                                accumulated_text.append(text_val.strip())
                    elif part.get("type") == "reasoning":
                        part_types[part.get("id", "")] = "reasoning"

                elif event_type == "message.part.delta":
                    part_id = props.get("partID", "")
                    field = props.get("field", "")
                    delta = props.get("delta", "")
                    msg_id = props.get("messageID", "")
                    if (
                        part_types.get(part_id) == "text"
                        and field == "text"
                        and delta
                        and msg_id in assistant_msg_ids
                    ):
                        accumulated_text.append(delta)
                        if self.progress.stream_preview:
                            await emit({"kind": "text", "text": delta})

                elif event_type == "permission.asked":
                    pass

                elif event_type == "session.idle":
                    final_text = "".join(accumulated_text).strip()
                    if final_text and not self.progress.stream_preview:
                        await emit({"kind": "text", "text": final_text})
                    return {"stopReason": "end_turn"}

                elif event_type == "session.error":
                    error = (
                        props.get("error") or props.get("message") or "unknown error"
                    )
                    raise RuntimeError(error)
        finally:
            self._active_emitters.pop(session_id, None)
            self._subscribers[session_id].remove(queue)
            if not self._subscribers[session_id]:
                del self._subscribers[session_id]

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
        if self._tool_event_task:
            self._tool_event_task.cancel()
            await asyncio.gather(self._tool_event_task, return_exceptions=True)
            self._tool_event_task = None
        if self._dispatcher:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
            self._dispatcher = None
        await self._client.close()
