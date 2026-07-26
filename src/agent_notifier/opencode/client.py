"""Async HTTP+SSE client for opencode serve."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

from aiohttp import ClientSession, ClientTimeout


class OpencodeClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._session: ClientSession | None = None
        self._sse_task: asyncio.Task | None = None
        self._events: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> None:
        self._session = ClientSession(timeout=ClientTimeout(total=None, sock_read=300))
        await self._ping()

    async def _ping(self) -> None:
        async with self._session.get(f"{self.base_url}/project/current") as resp:
            if resp.status >= 400:
                raise RuntimeError(
                    f"opencode server at {self.base_url} returned {resp.status}"
                )

    async def start_sse(self) -> None:
        if self._sse_task is not None:
            return
        self._sse_task = asyncio.create_task(self._sse_loop())

    async def _sse_loop(self) -> None:
        try:
            async with self._session.get(
                f"{self.base_url}/event",
                headers={"Accept": "text/event-stream"},
                timeout=ClientTimeout(total=None, sock_read=300),
            ) as resp:
                buf = ""
                async for chunk in resp.content.iter_any():
                    buf += chunk.decode("utf-8", errors="replace")
                    while "\n\n" in buf:
                        block, buf = buf.split("\n\n", 1)
                        for line in block.split("\n"):
                            if line.startswith("data: "):
                                try:
                                    payload = json.loads(line[6:])
                                except json.JSONDecodeError:
                                    continue
                                await self._events.put(payload)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            await self._events.put({"type": "_error", "error": str(exc)})

    async def next_event(self) -> dict:
        return await self._events.get()

    async def create_session(self) -> dict:
        async with self._session.post(
            f"{self.base_url}/session",
            json={},
        ) as resp:
            data = await resp.json()
            if resp.status >= 400:
                raise RuntimeError(
                    f"create session failed: {data.get('error', resp.status)}"
                )
            return data

    async def get_sessions(self) -> list[dict]:
        async with self._session.get(f"{self.base_url}/session") as resp:
            data = await resp.json()
            if resp.status >= 400:
                raise RuntimeError(
                    f"get sessions failed: {data.get('error', resp.status)}"
                )
            return data

    async def send_prompt(
        self, session_id: str, text: str
    ) -> dict:
        async with self._session.post(
            f"{self.base_url}/session/{session_id}/message",
            json={
                "parts": [{"type": "text", "text": text}],
            },
        ) as resp:
            data = await resp.json()
            if resp.status >= 400:
                raise RuntimeError(
                    f"send prompt failed: {data.get('error', resp.status)}"
                )
            return data

    async def reply_permission(
        self, session_id: str, permission_id: str, response: str
    ) -> dict:
        async with self._session.post(
            f"{self.base_url}/session/{session_id}/permissions/{permission_id}",
            json={"response": response},
        ) as resp:
            data = await resp.json()
            if resp.status >= 400:
                raise RuntimeError(
                    f"reply permission failed: {data.get('error', resp.status)}"
                )
            return data

    async def get_messages(
        self, session_id: str, limit: int = 50
    ) -> list[dict]:
        async with self._session.get(
            f"{self.base_url}/session/{session_id}/message",
            params={"limit": str(limit)},
        ) as resp:
            data = await resp.json()
            if resp.status >= 400:
                raise RuntimeError(
                    f"get messages failed: {data.get('error', resp.status)}"
                )
            return data

    async def close(self) -> None:
        if self._sse_task:
            self._sse_task.cancel()
            await asyncio.gather(self._sse_task, return_exceptions=True)
            self._sse_task = None
        if self._session:
            await self._session.close()
            self._session = None