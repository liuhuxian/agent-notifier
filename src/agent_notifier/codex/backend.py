"""High-level Codex operations used by the ACP adapter."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Protocol

from agent_notifier.config import ProgressConfig
from agent_notifier.registry import SessionRegistry


class RPCClient(Protocol):
    async def call(self, method: str, params: dict) -> dict: ...
    async def next_event(self) -> dict: ...


class CodexBackend:
    def __init__(
        self,
        rpc: RPCClient,
        registry: SessionRegistry,
        progress: ProgressConfig | None = None,
    ):
        self.rpc = rpc
        self.registry = registry
        self.progress = progress or ProgressConfig()
        self.active_turns: dict[str, str] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._dispatcher: asyncio.Task | None = None

    def _ensure_dispatcher(self) -> None:
        if self._dispatcher is None:
            self._dispatcher = asyncio.create_task(self._dispatch_events())

    def _register_transport_thread(
        self,
        project: str,
        external_key: str,
        thread_id: str,
        cwd: str,
    ) -> None:
        active = self.registry.get("cc_connect", project, external_key)
        if active is None:
            self.registry.bind(
                "cc_connect", project, external_key, thread_id, cwd
            )
        else:
            self.registry.subscribe(
                "cc_connect", project, external_key, thread_id, cwd
            )

    async def _dispatch_events(self) -> None:
        while True:
            event = await self.rpc.next_event()
            method = event.get("method")
            params = event.get("params") or {}
            if method == "agent-notifier/connectionClosed":
                for queues in list(self._subscribers.values()):
                    for queue in list(queues):
                        await queue.put(event)
                return
            thread_id = params.get("threadId")
            if method == "turn/started" and thread_id:
                turn = params.get("turn") or {}
                if turn.get("id"):
                    self.active_turns[thread_id] = turn["id"]
            elif method == "turn/completed" and thread_id:
                self.active_turns.pop(thread_id, None)
            if thread_id:
                for queue in list(self._subscribers.get(thread_id, [])):
                    await queue.put(event)

    async def start_thread(self, cwd: str, project: str, external_key: str) -> str:
        result = await self.rpc.call(
            "thread/start",
            {
                "cwd": cwd,
                "approvalPolicy": "on-request",
                "threadSource": "user",
            },
        )
        thread_id = result["thread"]["id"]
        self._register_transport_thread(
            project, external_key, thread_id, cwd
        )
        return thread_id

    async def resume_thread(
        self, thread_id: str, cwd: str, project: str, external_key: str
    ) -> str:
        target_thread_id = self.resolve_active_thread(
            project, external_key, thread_id
        )
        result = await self.rpc.call(
            "thread/resume",
            {"threadId": target_thread_id, "cwd": cwd},
        )
        resumed_id = result["thread"]["id"]
        if resumed_id != target_thread_id:
            self.registry.bind(
                "cc_connect", project, external_key, resumed_id, cwd
            )
        else:
            self._register_transport_thread(
                project, external_key, resumed_id, cwd
            )
        return resumed_id

    def resolve_active_thread(
        self, project: str, external_key: str, fallback: str
    ) -> str:
        mapping = self.registry.get("cc_connect", project, external_key)
        return mapping.thread_id if mapping else fallback

    async def prompt(
        self,
        thread_id: str,
        text: str,
        origin: str,
        emit: Callable[[dict], Awaitable[None]],
    ) -> dict:
        self._ensure_dispatcher()
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[thread_id].append(queue)
        input_items = [{"type": "text", "text": text, "text_elements": []}]
        final_text: str | None = None
        fallback_text: str | None = None
        item_phases: dict[str, str | None] = {}
        streamed_final = False
        try:
            active_turn = self.active_turns.get(thread_id)
            if active_turn:
                result = await self.rpc.call(
                    "turn/steer",
                    {
                        "threadId": thread_id,
                        "expectedTurnId": active_turn,
                        "input": input_items,
                    },
                )
                turn_id = result["turnId"]
            else:
                result = await self.rpc.call(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": input_items,
                    },
                )
                turn_id = result["turn"]["id"]
                self.active_turns[thread_id] = turn_id

            while True:
                event = await queue.get()
                params = event.get("params") or {}
                if event.get("method") == "agent-notifier/connectionClosed":
                    raise RuntimeError(
                        params.get("message")
                        or "Codex App Server connection closed"
                    )
                if params.get("turnId") not in {None, turn_id}:
                    continue
                method = event.get("method")
                if method == "turn/started":
                    if self.progress.progress_card:
                        await emit({"kind": "status", "text": "正在思考"})
                elif method == "item/started":
                    item = params.get("item") or {}
                    item_id = item.get("id")
                    if item_id:
                        item_phases[item_id] = item.get("phase")
                    if (
                        self.progress.progress_card
                        and item.get("type") == "commandExecution"
                        and item_id
                    ):
                        command = item.get("command") or ""
                        if isinstance(command, list):
                            command = " ".join(
                                str(part) for part in command
                            )
                        command = str(command)
                        await emit({
                            "kind": "tool_start",
                            "tool_call_id": item_id,
                            "title": self._tool_title(command),
                            "tool_kind": "execute",
                            "raw_input": {"command": command},
                        })
                elif method == "item/agentMessage/delta":
                    item_id = params.get("itemId")
                    delta = params.get("delta")
                    if (
                        self.progress.stream_preview
                        and delta
                        and item_phases.get(item_id) == "final_answer"
                    ):
                        streamed_final = True
                        await emit({"kind": "text", "text": delta})
                elif method == "item/completed":
                    item = params.get("item") or {}
                    item_id = item.get("id")
                    if (
                        self.progress.progress_card
                        and item.get("type") == "commandExecution"
                        and item_id
                    ):
                        status = item.get("status")
                        await emit({
                            "kind": "tool_complete",
                            "tool_call_id": item_id,
                            "status": (
                                "failed"
                                if status in {"failed", "declined"}
                                else "completed"
                            ),
                        })
                        continue
                    if item.get("type") != "agentMessage":
                        continue
                    item_text = item.get("text")
                    if not item_text:
                        continue
                    if item.get("phase") == "final_answer":
                        final_text = item_text
                    elif item.get("phase") is None:
                        fallback_text = item_text
                elif event.get("method") == "turn/completed":
                    turn = params.get("turn") or {}
                    if turn.get("id") != turn_id:
                        continue
                    status = turn.get("status")
                    if status == "completed":
                        response_text = final_text or fallback_text
                        if response_text and not streamed_final:
                            await emit(
                                {"kind": "text", "text": response_text}
                            )
                        return {"stopReason": "end_turn"}
                    error = turn.get("error") or {}
                    message = error.get("message")
                    if message:
                        raise RuntimeError(message)
                    return {"stopReason": "cancelled"}
        finally:
            self._subscribers[thread_id].remove(queue)
            if not self._subscribers[thread_id]:
                del self._subscribers[thread_id]

    @staticmethod
    def _tool_title(command: str) -> str:
        lowered = command.lower()
        test_markers = (
            "pytest",
            "unittest",
            "smoke_test",
            "run_all_verifications",
        )
        if any(marker in lowered for marker in test_markers):
            return "正在运行测试"
        return "正在执行工具"

    async def cancel(self, thread_id: str) -> None:
        turn_id = self.active_turns.get(thread_id)
        if turn_id:
            await self.rpc.call(
                "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
            )

    async def close(self) -> None:
        if self._dispatcher:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
            self._dispatcher = None
