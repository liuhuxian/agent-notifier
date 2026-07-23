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
from .routing import ClientEventHub


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
    request: dict
    resolved: bool = False
    resolution_notified: bool = False


def approval_short_id(token: str) -> str:
    return token.rsplit(":", 1)[-1][:10]


def approval_result(request: dict, decision: str) -> dict:
    method = request.get("method")
    params = request.get("params") or {}
    allowed = decision == "allow"
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


class ApprovalFanout:
    def __init__(
        self,
        store: ApprovalStore | None = None,
        event_hub: ClientEventHub | None = None,
    ):
        self.event_hub = event_hub or ClientEventHub()
        self.pending: dict[str, PendingApproval] = {}
        self.lock = asyncio.Lock()
        self.store = store

    async def add_client(self, client_id: str, ws: web.WebSocketResponse) -> None:
        await self.event_hub.add_client(client_id, ws)

    async def remove_client(self, client_id: str) -> None:
        await self.event_hub.remove_client(client_id)

    async def publish(
        self,
        message: dict,
        upstream,
        source_client_id: str | None = None,
        exclude_client_id: str | None = None,
    ) -> str:
        token = f"agent-notifier-approval:{uuid.uuid4()}"
        routed = dict(message)
        routed["id"] = token
        async with self.lock:
            self.pending[token] = PendingApproval(upstream, message["id"], message)
        if self.store:
            params = message.get("params") or {}
            self.store.register(
                token,
                params.get("threadId") or "unknown",
                message.get("method") or "approval",
                message,
            )
        thread_id = (message.get("params") or {}).get("threadId")
        await self.event_hub.broadcast(
            routed,
            thread_id,
            source_client_id,
            exclude_client_id=exclude_client_id,
        )
        return token

    async def route_upstream_resolution(
        self, message: dict, upstream: object
    ) -> bool:
        if message.get("method") != "serverRequest/resolved":
            return False
        params = message.get("params") or {}
        original_id = params.get("requestId")
        async with self.lock:
            match = next(
                (
                    (token, pending)
                    for token, pending in self.pending.items()
                    if pending.upstream is upstream
                    and pending.original_id == original_id
                ),
                None,
            )
            if match is None:
                return False
            token, pending = match
            if pending.resolution_notified:
                return True
            pending.resolved = True
            pending.resolution_notified = True
            thread_id = (pending.request.get("params") or {}).get("threadId")
        routed = dict(message)
        routed["params"] = dict(params)
        routed["params"]["requestId"] = token
        if thread_id and not routed["params"].get("threadId"):
            routed["params"]["threadId"] = thread_id
        await self.event_hub.broadcast(routed, thread_id)
        return True

    async def _claim(
        self, token: str, decision: str, resolved_by: str
    ) -> PendingApproval | None:
        async with self.lock:
            pending = self.pending.get(token)
            if pending is None:
                raise KeyError(token)
            if pending.resolved:
                return None
            if self.store:
                try:
                    self.store.resolve(token, decision, resolved_by)
                except ApprovalAlreadyResolved:
                    pending.resolved = True
                    return None
            pending.resolved = True
            return pending

    async def resolve(self, message: dict, resolved_by: str) -> bool:
        token = message.get("id")
        if not isinstance(token, str) or not token.startswith("agent-notifier-approval:"):
            return False
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
            pending = await self._claim(
                token, "allow" if accepted else "deny", resolved_by
            )
        except KeyError:
            return True
        if pending is None:
            return True
        routed = dict(message)
        routed["id"] = pending.original_id
        await pending.upstream.send_json(routed)
        return True

    async def resolve_external(
        self, approval_id: str, decision: str, resolved_by: str = "feishu"
    ) -> str:
        if decision not in {"allow", "deny"}:
            raise ValueError(f"invalid approval decision: {decision}")
        async with self.lock:
            if approval_id.startswith("agent-notifier-approval:"):
                candidates = [approval_id] if approval_id in self.pending else []
            else:
                candidates = [
                    token
                    for token in self.pending
                    if token.rsplit(":", 1)[-1].startswith(approval_id)
                ]
        if not candidates:
            raise KeyError(approval_id)
        if len(candidates) > 1:
            raise ValueError(f"ambiguous approval id: {approval_id}")
        pending = await self._claim(candidates[0], decision, resolved_by)
        if pending is None:
            return "already_resolved"
        await pending.upstream.send_json(
            {
                "jsonrpc": "2.0",
                "id": pending.original_id,
                "result": approval_result(pending.request, decision),
            }
        )
        return "resolved"


class AppServerProxy:
    def __init__(
        self,
        listen_socket: Path,
        upstream_socket: Path,
        on_terminal_completion: Callable[[str, str], Awaitable[None]] | None = None,
        on_remote_completion: Callable[[str, str], Awaitable[None]] | None = None,
        on_remote_progress: Callable[[str, str], Awaitable[None]] | None = None,
        approval_store: ApprovalStore | None = None,
        on_approval_request: Callable[[str, dict], Awaitable[None]] | None = None,
    ):
        self.listen_socket = Path(listen_socket)
        self.upstream_socket = Path(upstream_socket)
        self.event_hub = ClientEventHub()
        self.fanout = ApprovalFanout(approval_store, self.event_hub)
        self.runner: web.AppRunner | None = None
        self.on_terminal_completion = on_terminal_completion
        self.on_remote_completion = on_remote_completion
        self.on_remote_progress = on_remote_progress
        self.on_approval_request = on_approval_request
        self._thread_origins: dict[str, str] = {}
        self._turn_text: dict[tuple[str, str], list[str]] = {}
        self._turn_last_message: dict[tuple[str, str], str] = {}
        self._turn_final_text: dict[tuple[str, str], str] = {}
        self._remote_progress_items: dict[tuple[str, str], set[str]] = {}
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
        app.router.add_post("/approval", self._approval_decision)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.UnixSite(self.runner, str(self.listen_socket)).start()
        os.chmod(self.listen_socket, 0o600)

    async def _approval_decision(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            approval_id = str(payload.get("approval_id") or "")
            decision = str(payload.get("decision") or "")
            if not approval_id:
                raise ValueError("approval_id is required")
            status = await self.fanout.resolve_external(approval_id, decision)
            return web.json_response({"status": status, "approval_id": approval_id})
        except KeyError:
            return web.json_response(
                {"status": "not_found", "error": "approval request not found"},
                status=404,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return web.json_response(
                {"status": "invalid", "error": str(exc)},
                status=400,
            )

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
            pending_request_methods: dict[object, str] = {}

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
                        await self.event_hub.subscribe_from_request(
                            client_id, payload
                        )
                        await self.event_hub.register_turn_request(
                            (payload.get("params") or {}).get("threadId"),
                            client_id,
                            payload.get("method") or "",
                        )
                        if "id" in payload and payload.get("method"):
                            pending_request_methods[payload["id"]] = payload["method"]
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
                        if "method" not in payload and "id" in payload:
                            request_method = pending_request_methods.pop(
                                payload["id"], None
                            )
                            await self.event_hub.subscribe_from_response(
                                client_id, request_method, payload
                            )
                        if payload.get("method") in APPROVAL_METHODS and "id" in payload:
                            if not await self.event_hub.is_authoritative_source(
                                payload, client_id
                            ):
                                continue
                            token = await self.fanout.publish(
                                payload,
                                upstream,
                                client_id,
                                exclude_client_id=(
                                    client_id
                                    if client_kind == "cc_connect"
                                    else None
                                ),
                            )
                            if self.on_approval_request:
                                try:
                                    await self.on_approval_request(token, payload)
                                except Exception:
                                    logger.exception(
                                        "failed to send remote approval notification: "
                                        "approval=%s",
                                        approval_short_id(token),
                                    )
                        elif await self.fanout.route_upstream_resolution(
                            payload, upstream
                        ):
                            continue
                        else:
                            if payload.get("method"):
                                routed = await self.event_hub.route_notification(
                                    payload, client_id
                                )
                                if routed:
                                    await self._observe_notification(payload)
                            elif not downstream.closed:
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

    async def _observe_notification(self, payload: dict) -> None:
        method = payload.get("method")
        params = payload.get("params") or {}
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        origin = self._thread_origins.get(thread_id) if thread_id else None
        if method == "item/agentMessage/delta" and thread_id and turn_id:
            self._turn_text.setdefault((thread_id, turn_id), []).append(
                params.get("delta", "")
            )
            return
        if method == "item/completed" and thread_id and turn_id:
            item = params.get("item") or {}
            if item.get("type") != "agentMessage":
                return
            key = (thread_id, turn_id)
            text = item.get("text", "")
            self._turn_last_message[key] = text
            phase = item.get("phase")
            if phase == "final_answer":
                self._turn_final_text[key] = text
            elif (
                phase == "commentary"
                and origin == "cc_connect"
                and text
                and self.on_remote_progress
            ):
                item_id = str(item.get("id") or "")
                sent = self._remote_progress_items.setdefault(key, set())
                if item_id not in sent:
                    sent.add(item_id)
                    try:
                        await self.on_remote_progress(thread_id, text)
                    except Exception:
                        logger.exception(
                            "failed to send remote progress: thread=%s turn=%s item=%s",
                            thread_id,
                            turn_id,
                            item_id,
                        )
            return
        if method != "turn/completed" or not thread_id:
            return
        turn = params.get("turn") or {}
        turn_id = turn.get("id")
        if not turn_id or turn_id in self._completed_turns:
            return
        if len(self._completed_order) >= self._completed_limit:
            self._completed_turns.discard(self._completed_order.popleft())
        self._completed_order.append(turn_id)
        self._completed_turns.add(turn_id)
        key = (thread_id, turn_id)
        delta_text = "".join(self._turn_text.pop(key, []))
        last_message = self._turn_last_message.pop(key, None)
        final_text = self._turn_final_text.pop(key, None)
        self._remote_progress_items.pop(key, None)
        text = final_text if final_text is not None else (
            last_message if last_message is not None else delta_text
        )
        origin = self._thread_origins.pop(thread_id, "terminal")
        if should_send_completion_notification(origin) and self.on_terminal_completion:
            await self.on_terminal_completion(thread_id, text)
        elif origin == "cc_connect" and self.on_remote_completion:
            await self.on_remote_completion(thread_id, text)

    async def close(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        if self.listen_socket.exists():
            self.listen_socket.unlink()
