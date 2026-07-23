"""Async JSON-RPC client for Codex App Server through the notifier proxy."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable
from pathlib import Path

from aiohttp import ClientSession, UnixConnector, WSMsgType


ServerRequestHandler = Callable[[str, dict], Awaitable[dict]]


class CodexRPCError(RuntimeError):
    pass


class CodexAppServerClient:
    def __init__(self, socket_path: Path, client_name: str):
        self.socket_path = Path(socket_path)
        self.client_name = client_name
        self._ids = itertools.count(1)
        self._pending: dict[object, asyncio.Future] = {}
        self._events: asyncio.Queue = asyncio.Queue()
        self._session: ClientSession | None = None
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._server_request_handler: ServerRequestHandler | None = None

    def set_server_request_handler(self, handler: ServerRequestHandler) -> None:
        self._server_request_handler = handler

    async def connect(self) -> None:
        connector = UnixConnector(path=str(self.socket_path))
        self._session = ClientSession(connector=connector)
        try:
            self._ws = await self._session.ws_connect(
                f"http://localhost/?client={self.client_name}", heartbeat=30
            )
        except Exception:
            await self._session.close()
            self._session = None
            raise
        self._reader = asyncio.create_task(self._read_loop())
        try:
            await self.call(
                "initialize",
                {
                    "clientInfo": {
                        "name": "agent-notifier",
                        "title": "Agent Notifier",
                        "version": "0.1.0",
                    },
                    "capabilities": {
                        "experimentalApi": False,
                        "requestAttestation": False,
                    },
                },
            )
            await self.notify("initialized")
        except Exception:
            await self.close()
            raise

    async def _read_loop(self) -> None:
        try:
            async for message in self._ws:
                if message.type != WSMsgType.TEXT:
                    continue
                payload = message.json()
                if "method" in payload and "id" in payload:
                    asyncio.create_task(self._handle_server_request(payload))
                elif "method" in payload:
                    await self._events.put(payload)
                elif "id" in payload:
                    future = self._pending.pop(payload["id"], None)
                    if future and not future.done():
                        if "error" in payload:
                            error = payload["error"]
                            future.set_exception(
                                CodexRPCError(
                                    f"{error.get('code')}: {error.get('message')}"
                                )
                            )
                        else:
                            future.set_result(payload.get("result") or {})
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CodexRPCError("connection closed"))

    async def _handle_server_request(self, payload: dict) -> None:
        try:
            if self._server_request_handler is None:
                raise CodexRPCError("no approval client is connected")
            result = await self._server_request_handler(
                payload["method"], payload.get("params") or {}
            )
            response = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": payload["id"],
                "error": {"code": -32000, "message": str(exc)},
            }
        await self._ws.send_json(response)

    async def call(self, method: str, params: dict) -> dict:
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._ws.send_json(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._ws.send_json(payload)

    async def next_event(self) -> dict:
        return await self._events.get()

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(CodexRPCError("connection closed"))
        self._pending.clear()
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
