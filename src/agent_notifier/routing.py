"""Thread-scoped event routing for App Server clients."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field


@dataclass
class ClientRoute:
    socket: object
    threads: set[str] = field(default_factory=set)


def event_thread_id(payload: dict) -> str | None:
    params = payload.get("params") or {}
    thread_id = params.get("threadId")
    if thread_id:
        return str(thread_id)
    thread = params.get("thread") or {}
    return str(thread["id"]) if thread.get("id") else None


def event_turn_id(payload: dict) -> str | None:
    params = payload.get("params") or {}
    turn_id = params.get("turnId")
    if turn_id:
        return str(turn_id)
    turn = params.get("turn") or {}
    return str(turn["id"]) if turn.get("id") else None


class ClientEventHub:
    """Broadcast one authoritative event stream to clients on the same thread."""

    def __init__(self, completed_limit: int = 10_000):
        self.clients: dict[str, ClientRoute] = {}
        self.thread_sources: dict[str, str] = {}
        self.turn_sources: dict[tuple[str, str], str] = {}
        self.completed_turns: set[tuple[str, str]] = set()
        self.completed_order: deque[tuple[str, str]] = deque()
        self.completed_limit = completed_limit
        self.lock = asyncio.Lock()

    async def add_client(self, client_id: str, socket: object) -> None:
        async with self.lock:
            self.clients[client_id] = ClientRoute(socket)

    async def remove_client(self, client_id: str) -> None:
        async with self.lock:
            self.clients.pop(client_id, None)
            abandoned = [
                key
                for key, source in self.turn_sources.items()
                if source == client_id and key not in self.completed_turns
            ]
            for key in abandoned:
                self.turn_sources.pop(key, None)
            abandoned_threads = [
                thread_id
                for thread_id, source in self.thread_sources.items()
                if source == client_id
            ]
            for thread_id in abandoned_threads:
                self.thread_sources.pop(thread_id, None)

    async def subscribe(self, client_id: str, thread_id: str | None) -> None:
        if not thread_id:
            return
        async with self.lock:
            route = self.clients.get(client_id)
            if route:
                route.threads.add(str(thread_id))

    async def subscribe_from_request(self, client_id: str, payload: dict) -> None:
        params = payload.get("params") or {}
        await self.subscribe(client_id, params.get("threadId"))

    async def register_turn_request(
        self, thread_id: str | None, client_id: str, method: str
    ) -> None:
        if not thread_id or method not in {"turn/start", "turn/steer"}:
            return
        async with self.lock:
            if method == "turn/start":
                self.thread_sources[str(thread_id)] = client_id
            else:
                self.thread_sources.setdefault(str(thread_id), client_id)

    async def subscribe_from_response(
        self, client_id: str, request_method: str | None, payload: dict
    ) -> None:
        if request_method not in {"thread/start", "thread/resume", "thread/read"}:
            return
        result = payload.get("result") or {}
        thread = result.get("thread") or {}
        await self.subscribe(client_id, thread.get("id"))

    async def _targets(
        self,
        thread_id: str | None,
        source_client_id: str | None = None,
        exclude_client_id: str | None = None,
    ) -> list[object]:
        async with self.lock:
            if thread_id:
                subscribed = [
                    (client_id, route.socket)
                    for client_id, route in self.clients.items()
                    if thread_id in route.threads
                ]
                if subscribed:
                    return [
                        socket
                        for client_id, socket in subscribed
                        if client_id != exclude_client_id
                    ]
            if (
                source_client_id
                and source_client_id in self.clients
                and source_client_id != exclude_client_id
            ):
                return [self.clients[source_client_id].socket]
            return [
                route.socket
                for client_id, route in self.clients.items()
                if client_id != exclude_client_id
            ]

    async def broadcast(
        self,
        payload: dict,
        thread_id: str | None,
        source_client_id: str | None = None,
        exclude_client_id: str | None = None,
    ) -> None:
        for socket in await self._targets(
            thread_id, source_client_id, exclude_client_id
        ):
            if not socket.closed:
                await socket.send_json(payload)

    async def route_notification(
        self, payload: dict, source_client_id: str
    ) -> bool:
        thread_id = event_thread_id(payload)
        if not thread_id:
            await self.broadcast(payload, None, source_client_id)
            return True

        turn_id = event_turn_id(payload)
        method = payload.get("method")
        if turn_id:
            key = (thread_id, turn_id)
            async with self.lock:
                if key in self.completed_turns:
                    return False
                source = self.turn_sources.get(key)
                if source is None:
                    source = self.thread_sources.get(
                        thread_id, source_client_id
                    )
                    self.turn_sources[key] = source
                if source != source_client_id:
                    return False
                if method == "turn/completed":
                    if len(self.completed_order) >= self.completed_limit:
                        expired = self.completed_order.popleft()
                        self.completed_turns.discard(expired)
                        self.turn_sources.pop(expired, None)
                    self.completed_order.append(key)
                    self.completed_turns.add(key)

        await self.broadcast(payload, thread_id, source_client_id)
        return True

    async def is_authoritative_source(
        self, payload: dict, source_client_id: str
    ) -> bool:
        thread_id = event_thread_id(payload)
        if not thread_id:
            return True
        turn_id = event_turn_id(payload)
        async with self.lock:
            source = None
            if turn_id:
                source = self.turn_sources.get((thread_id, turn_id))
            if source is None:
                source = self.thread_sources.get(thread_id)
            if source is None:
                source = source_client_id
                self.thread_sources[thread_id] = source
            if turn_id:
                self.turn_sources.setdefault((thread_id, turn_id), source)
            return source == source_client_id
