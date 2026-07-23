"""Unix WebSocket proxy for a shared Codex App Server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from aiohttp import ClientSession, UnixConnector, WSMsgType, web

from .approvals import ApprovalAlreadyResolved, ApprovalStore
from .policy import should_send_completion_notification


logger = logging.getLogger(__name__)


APPROVAL_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
}


@dataclass
class PendingApproval:
    upstream: object
    original_id: object
    resolved: bool = False


class ApprovalFanout:
    def __init__(self, store: ApprovalStore | None = None):
        self.clients: dict[str, web.WebSocketResponse] = {}
        self.pending: dict[str, PendingApproval] = {}
        self.lock = asyncio.Lock()
        self.store = store

    async def add_client(self, client_id: str, ws: web.WebSocketResponse) -> None:
        async with self.lock:
            self.clients[client_id] = ws

    async def remove_client(self, client_id: str) -> None:
        async with self.lock:
            self.clients.pop(client_id, None)

    async def publish(self, message: dict, upstream) -> None:
        token = f"agent-notifier-approval:{uuid.uuid4()}"
        routed = dict(message)
        routed["id"] = token
        async with self.lock:
            self.pending[token] = PendingApproval(upstream, message["id"])
            clients = list(self.clients.values())
        if self.store:
            params = message.get("params") or {}
            self.store.register(
                token,
                params.get("threadId") or "unknown",
                message.get("method") or "approval",
                message,
            )
        for client in clients:
            if not client.closed:
                await client.send_json(routed)

    async def resolve(self, message: dict, resolved_by: str) -> bool:
        token = message.get("id")
        if not isinstance(token, str) or not token.startswith("agent-notifier-approval:"):
            return False
        async with self.lock:
            pending = self.pending.get(token)
            if pending is None or pending.resolved:
                return True
            if self.store:
                result = message.get("result") or {}
                accepted = result.get("decision") in {
                    "accept",
                    "acceptForSession",
                    "approved",
                    "approved_for_session",
                }
                if isinstance(result.get("decision"), dict):
                    accepted = any(
                        key in result["decision"]
                        for key in (
                            "acceptWithExecpolicyAmendment",
                            "applyNetworkPolicyAmendment",
                            "approved_execpolicy_amendment",
                            "network_policy_amendment",
                        )
                    )
                if "permissions" in result:
                    accepted = bool(result.get("permissions"))
                try:
                    self.store.resolve(
                        token, "allow" if accepted else "deny", resolved_by
                    )
                except ApprovalAlreadyResolved:
                    pending.resolved = True
                    return True
            pending.resolved = True
        routed = dict(message)
        routed["id"] = pending.original_id
        await pending.upstream.send_json(routed)
        return True


class AppServerProxy:
    def __init__(
        self,
        listen_socket: Path,
        upstream_socket: Path,
        on_terminal_completion: Callable[[str, str], Awaitable[None]] | None = None,
        approval_store: ApprovalStore | None = None,
    ):
        self.listen_socket = Path(listen_socket)
        self.upstream_socket = Path(upstream_socket)
        self.fanout = ApprovalFanout(approval_store)
        self.runner: web.AppRunner | None = None
        self.on_terminal_completion = on_terminal_completion
        self._thread_origins: dict[str, str] = {}
        self._turn_text: dict[tuple[str, str], list[str]] = {}
        self._completed_turns: set[str] = set()
        self._completed_order: deque[str] = deque()
        self._completed_limit = 10_000

    async def start(self) -> None:
        self.listen_socket.parent.mkdir(parents=True, exist_ok=True)
        if self.listen_socket.exists():
            self.listen_socket.unlink()
        app = web.Application()
        app.router.add_get("/", self._websocket)
        app.router.add_get("/rpc", self._websocket)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.UnixSite(self.runner, str(self.listen_socket)).start()
        os.chmod(self.listen_socket, 0o600)

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        downstream = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
        await downstream.prepare(request)
        client_kind = request.query.get("client") or "terminal"
        client_id = f"{client_kind}:{uuid.uuid4()}"
        await self.fanout.add_client(client_id, downstream)

        connector = UnixConnector(path=str(self.upstream_socket))
        session = ClientSession(connector=connector)
        try:
            upstream = await session.ws_connect(
                "http://localhost/", heartbeat=30, max_msg_size=0
            )

            async def downstream_to_upstream() -> None:
                message_type = None
                method = None
                request_id = None
                try:
                    async for message in downstream:
                        message_type = message.type
                        if message.type != WSMsgType.TEXT:
                            continue
                        payload = json.loads(message.data)
                        if isinstance(payload, dict):
                            method = payload.get("method")
                            request_id = payload.get("id")
                        if await self.fanout.resolve(payload, client_kind):
                            continue
                        if payload.get("method") in {"turn/start", "turn/steer"}:
                            thread_id = (payload.get("params") or {}).get("threadId")
                            if thread_id:
                                self._thread_origins[thread_id] = client_kind
                        await upstream.send_json(payload)
                except Exception:
                    logger.exception(
                        "proxy downstream->upstream failed: client=%s type=%s method=%s id=%r",
                        client_kind,
                        message_type,
                        method,
                        request_id,
                    )
                    raise
            async def upstream_to_downstream() -> None:
                message_type = None
                method = None
                request_id = None
                try:
                    async for message in upstream:
                        message_type = message.type
                        if message.type != WSMsgType.TEXT:
                            continue
                        payload = json.loads(message.data)
                        if isinstance(payload, dict):
                            method = payload.get("method")
                            request_id = payload.get("id")
                        if payload.get("method") in APPROVAL_METHODS and "id" in payload:
                            await self.fanout.publish(payload, upstream)
                        else:
                            await self._observe_notification(payload, client_kind)
                            if not downstream.closed:
                                await downstream.send_json(payload)
                except Exception:
                    logger.exception(
                        "proxy upstream->downstream failed: client=%s type=%s method=%s id=%r",
                        client_kind,
                        message_type,
                        method,
                        request_id,
                    )
                    raise
            tasks = [
                asyncio.create_task(downstream_to_upstream()),
                asyncio.create_task(upstream_to_downstream()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
            await upstream.close()
        finally:
            await self.fanout.remove_client(client_id)
            await session.close()
            await downstream.close()
        return downstream

    async def _observe_notification(self, payload: dict, client_kind: str = "terminal") -> None:
        method = payload.get("method")
        params = payload.get("params") or {}
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        origin = self._thread_origins.get(thread_id) if thread_id else None
        if method == "item/agentMessage/delta" and thread_id and turn_id:
            if origin != client_kind:
                return
            self._turn_text.setdefault((thread_id, turn_id), []).append(
                params.get("delta", "")
            )
            return
        if method != "turn/completed" or not thread_id:
            return
        if origin != client_kind:
            return
        turn = params.get("turn") or {}
        turn_id = turn.get("id")
        if not turn_id or turn_id in self._completed_turns:
            return
        if len(self._completed_order) >= self._completed_limit:
            self._completed_turns.discard(self._completed_order.popleft())
        self._completed_order.append(turn_id)
        self._completed_turns.add(turn_id)
        text = "".join(self._turn_text.pop((thread_id, turn_id), []))
        origin = self._thread_origins.pop(thread_id, "terminal")
        if should_send_completion_notification(origin) and self.on_terminal_completion:
            await self.on_terminal_completion(thread_id, text)

    async def close(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        if self.listen_socket.exists():
            self.listen_socket.unlink()
