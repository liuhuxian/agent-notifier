"""Newline-delimited JSON-RPC 2.0 ACP stdio server."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import TextIO

from .handler import ACPHandler, ACPMethodError, AgentBackend


class ACPStdioServer:
    def __init__(self, backend: AgentBackend, stdin: TextIO, stdout: TextIO):
        self.stdin = stdin
        self.stdout = stdout
        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._pending: dict[object, asyncio.Future] = {}
        self.handler = ACPHandler(
            backend,
            cwd=os.getcwd(),
            project=os.environ.get("CC_PROJECT", "default"),
            external_key=os.environ.get("CC_SESSION_KEY", "default"),
            emit=self.notify,
        )

    async def write(self, payload: dict) -> None:
        async with self._write_lock:
            self.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.stdout.flush()

    async def notify(self, method: str, params: dict) -> None:
        await self.write({"jsonrpc": "2.0", "method": method, "params": params})

    async def call_client(self, method: str, params: dict, timeout: float = 900) -> dict:
        request_id = f"agent-notifier-{self._next_id}"
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self.write(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def handle_codex_request(self, method: str, params: dict) -> dict:
        tool_call_id = params.get("itemId") or params.get("approvalId") or "approval"
        title = params.get("reason") or method
        raw_input = {
            key: value
            for key, value in params.items()
            if key not in {"threadId", "turnId", "itemId"}
        }
        result = await self.call_client(
            "session/request_permission",
            {
                "sessionId": params.get("threadId") or self.handler.session_id,
                "toolCall": {
                    "toolCallId": tool_call_id,
                    "title": title,
                    "kind": "permission",
                    "rawInput": raw_input,
                },
                "options": [
                    {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "deny_once", "name": "Deny", "kind": "reject_once"},
                ],
            },
        )
        outcome = result.get("outcome") or {}
        allowed = outcome.get("outcome") == "selected" and outcome.get("optionId") == "allow_once"
        if method == "item/permissions/requestApproval":
            return {
                "permissions": params.get("permissions") if allowed else {},
                "scope": "turn",
            }
        if method in {"execCommandApproval", "applyPatchApproval"}:
            return {
                "decision": (
                    "approved"
                    if allowed
                    else {"denied": {"rejection": "Denied remotely"}}
                )
            }
        return {"decision": "accept" if allowed else "decline"}

    async def process(self, payload: dict) -> None:
        if "method" not in payload and "id" in payload:
            future = self._pending.get(payload["id"])
            if future and not future.done():
                if "error" in payload:
                    future.set_exception(RuntimeError(payload["error"].get("message", "ACP error")))
                else:
                    future.set_result(payload.get("result") or {})
            return
        if "method" not in payload or "id" not in payload:
            return
        try:
            result = await self.handler.request(payload["method"], payload.get("params") or {})
            await self.write({"jsonrpc": "2.0", "id": payload["id"], "result": result})
        except ACPMethodError as exc:
            await self.write(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "error": {"code": exc.code, "message": str(exc)},
                }
            )
        except Exception as exc:
            await self.write(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "error": {"code": -32000, "message": str(exc)},
                }
            )

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, self.stdin.readline)
            if not line:
                return
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            await self.process(payload)


async def serve_stdio(backend: AgentBackend) -> None:
    await ACPStdioServer(backend, sys.stdin, sys.stdout).run()
