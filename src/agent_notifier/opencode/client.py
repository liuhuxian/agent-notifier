"""Async HTTP+SSE client for opencode serve."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout


class OpencodeClient:
    _SSE_RECONNECT_INITIAL_S = 0.5
    _SSE_RECONNECT_MAX_S = 10.0

    def __init__(self, base_url: str, directory: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.directory = self._normalize_directory(directory)
        self._session: ClientSession | None = None
        self._sse_task: asyncio.Task | None = None
        self._events: asyncio.Queue = asyncio.Queue()

    @staticmethod
    def _normalize_directory(directory: str | None) -> str | None:
        if not directory:
            return None
        return str(Path(directory).expanduser().resolve())

    def _directory_params(self) -> dict[str, str] | None:
        if self.directory is None:
            return None
        return {"directory": self.directory}

    async def set_directory(self, directory: str | None) -> None:
        """Bind subsequent OpenCode API and SSE traffic to one project."""
        normalized = self._normalize_directory(directory)
        if normalized == self.directory:
            return
        was_running = self._sse_task is not None
        if was_running:
            self._sse_task.cancel()
            await asyncio.gather(self._sse_task, return_exceptions=True)
            self._sse_task = None
        self.directory = normalized
        if was_running:
            await self.start_sse()

    async def connect(self) -> None:
        # OpenCode may legitimately stay silent while a tool or model call is
        # running.  An idle read timeout would turn that silence into a false
        # transport failure and lose the eventual completion notification.
        self._session = ClientSession(timeout=ClientTimeout(total=None, sock_read=None))
        await self._ping()

    async def _ping(self) -> None:
        async with self._session.get(
            f"{self.base_url}/project/current",
            params=self._directory_params(),
        ) as resp:
            if resp.status >= 400:
                raise RuntimeError(
                    f"opencode server at {self.base_url} returned {resp.status}"
                )

    async def start_sse(self) -> None:
        if self._sse_task is not None:
            return
        self._sse_task = asyncio.create_task(self._sse_loop())

    async def _sse_loop(self) -> None:
        reconnect_delay = self._SSE_RECONNECT_INITIAL_S
        while True:
            try:
                async with self._session.get(
                    f"{self.base_url}/event",
                    headers={"Accept": "text/event-stream"},
                    params=self._directory_params(),
                    timeout=ClientTimeout(total=None, sock_read=None),
                ) as resp:
                    reconnect_delay = self._SSE_RECONNECT_INITIAL_S
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
                    await self._events.put({
                        "type": "_error",
                        "error": "OpenCode SSE connection closed",
                    })
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._events.put({"type": "_error", "error": str(exc)})

            # Keep the SSE task alive across OpenCode restarts.  The current
            # prompt receives the error and can fail explicitly; later prompts
            # use the re-established event stream instead of hanging forever.
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(
                reconnect_delay * 2, self._SSE_RECONNECT_MAX_S
            )

    async def next_event(self) -> dict:
        return await self._events.get()

    async def create_session(self) -> dict:
        async with self._session.post(
            f"{self.base_url}/session",
            params=self._directory_params(),
            json={},
        ) as resp:
            data = await resp.json()
            if resp.status >= 400:
                raise RuntimeError(
                    f"create session failed: {data.get('error', resp.status)}"
                )
            return data

    async def get_sessions(self) -> list[dict]:
        async with self._session.get(
            f"{self.base_url}/session",
            params=self._directory_params(),
        ) as resp:
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
            params=self._directory_params(),
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
            params=self._directory_params(),
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
            params={
                **({"directory": self.directory} if self.directory else {}),
                "limit": str(limit),
            },
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
