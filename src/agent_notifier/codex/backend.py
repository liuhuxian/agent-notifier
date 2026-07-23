"""High-level Codex operations used by the ACP adapter."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Protocol

from agent_notifier.registry import SessionRegistry


class RPCClient(Protocol):
    async def call(self, method: str, params: dict) -> dict: ...
    async def next_event(self) -> dict: ...


class CodexBackend:
    def __init__(self, rpc: RPCClient, registry: SessionRegistry):
        self.rpc = rpc
        self.registry = registry
        self.active_turns: dict[str, str] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._dispatcher: asyncio.Task | None = None

    def _ensure_dispatcher(self) -> None:
        if self._dispatcher is None:
            self._dispatcher = asyncio.create_task(self._dispatch_events())

    async def _dispatch_events(self) -> None:
        while True:
            event = await self.rpc.next_event()
            method = event.get("method")
            params = event.get("params") or {}
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
        self.registry.bind(
            "cc_connect", project, external_key, thread_id, cwd
        )
        return thread_id

    async def resume_thread(
        self, thread_id: str, cwd: str, project: str, external_key: str
    ) -> str:
        result = await self.rpc.call(
            "thread/resume",
            {"threadId": thread_id, "cwd": cwd},
        )
        resumed_id = result["thread"]["id"]
        self.registry.bind("cc_connect", project, external_key, resumed_id, cwd)
        return resumed_id

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
        metadata = {"agent_notifier_origin": origin}
        try:
            active_turn = self.active_turns.get(thread_id)
            if active_turn:
                result = await self.rpc.call(
                    "turn/steer",
                    {
                        "threadId": thread_id,
                        "expectedTurnId": active_turn,
                        "input": input_items,
                        "responsesapiClientMetadata": metadata,
                    },
                )
                turn_id = result["turnId"]
            else:
                result = await self.rpc.call(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": input_items,
                        "responsesapiClientMetadata": metadata,
                    },
                )
                turn_id = result["turn"]["id"]
                self.active_turns[thread_id] = turn_id

            while True:
                event = await queue.get()
                params = event.get("params") or {}
                if params.get("turnId") not in {None, turn_id}:
                    continue
                if event.get("method") == "item/agentMessage/delta":
                    delta = params.get("delta")
                    if delta:
                        await emit({"kind": "text", "text": delta})
                elif event.get("method") == "turn/completed":
                    turn = params.get("turn") or {}
                    if turn.get("id") != turn_id:
                        continue
                    status = turn.get("status")
                    return {
                        "stopReason": "end_turn" if status == "completed" else "cancelled"
                    }
        finally:
            self._subscribers[thread_id].remove(queue)
            if not self._subscribers[thread_id]:
                del self._subscribers[thread_id]

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
