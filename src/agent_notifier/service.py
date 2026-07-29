"""Shared Codex App Server and proxy process supervision."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import signal
import stat
import shutil
from pathlib import Path

from .approvals import ApprovalStore
from .config import NotifierConfig, Paths
from .feishu import (
    remove_message_reaction,
    reply_approval_result_card,
    send_approval_card,
    send_markdown_message,
    send_progress_message_with_onit,
    send_text_message,
)
from .native_hooks import format_result
from .notification_routing import resolve_thread_route_name
from .proxy import AppServerProxy, approval_short_id
from .registry import SessionRegistry


logger = logging.getLogger(__name__)


def approval_details(request: dict) -> tuple[str, str]:
    params = request.get("params") or {}
    reason = str(params.get("reason") or request.get("method") or "Codex 权限请求")
    command = params.get("command")
    if not command:
        actions = params.get("commandActions") or []
        if actions:
            command = actions[0].get("command")
    if not command:
        command = params.get("grantRoot") or params.get("permissions") or "(无命令摘要)"
    return reason[:800], str(command)[:1600]


def format_approval_message(token: str, request: dict) -> str:
    request_id = approval_short_id(token)
    reason, command = approval_details(request)
    return (
        f"Codex 权限审批 [{request_id}]\n"
        f"原因：{reason}\n"
        f"操作：{command}\n\n"
        f"允许：/codex-approve {request_id}\n"
        f"拒绝：/codex-deny {request_id}"
    )


def codex_app_server_command(paths: Paths, codex_binary: str = "codex") -> list[str]:
    return [
        codex_binary,
        "app-server",
        "-c",
        "notify=[]",
        "--listen",
        f"unix://{paths.upstream_socket}",
    ]


async def wait_for_socket(path: Path, process: asyncio.subprocess.Process, timeout: float = 20) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if process.returncode is not None:
            raise RuntimeError(f"Codex App Server exited with code {process.returncode}")
        try:
            if stat.S_ISSOCK(path.stat().st_mode):
                return
        except FileNotFoundError:
            pass
        await asyncio.sleep(0.1)
    raise TimeoutError(f"timed out waiting for App Server socket: {path}")


class SharedService:
    def __init__(self, paths: Paths, codex_binary: str = "codex"):
        self.paths = paths
        self.codex_binary = codex_binary
        self.config = NotifierConfig.load(paths.config_file)
        self.stop_event = asyncio.Event()
        self.process: asyncio.subprocess.Process | None = None
        self.proxy: AppServerProxy | None = None
        self.registry: SessionRegistry | None = None
        self.approval_store: ApprovalStore | None = None
        self._remote_progress_onit: dict[
            str, tuple[str, str, str]
        ] = {}

    async def run(self) -> None:
        self.paths.ensure_directories()
        lock = self.paths.lock_file.open("w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.close()
            raise RuntimeError("another agent-notifier service is already starting") from exc
        self.paths.pid_file.write_text(str(os.getpid()))
        self.registry = SessionRegistry(self.paths.state_db)
        self.approval_store = ApprovalStore(self.paths.state_db)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:
                pass
        backoff = 1.0
        try:
            while not self.stop_event.is_set():
                if self.paths.upstream_socket.exists():
                    self.paths.upstream_socket.unlink()
                self.process = await asyncio.create_subprocess_exec(
                    *codex_app_server_command(self.paths, self.codex_binary),
                    env={**os.environ, "AGENT_NOTIFIER_MANAGED": "1"},
                )
                try:
                    await wait_for_socket(self.paths.upstream_socket, self.process)
                    self.proxy = AppServerProxy(
                        self.paths.proxy_socket,
                        self.paths.upstream_socket,
                        on_terminal_completion=self._notify_terminal_completion,
                        on_remote_completion=self._finish_remote_progress,
                        on_remote_progress=self._notify_remote_progress,
                        approval_store=self.approval_store,
                        on_approval_request=self._notify_approval_request,
                        on_terminal_approval=self._notify_terminal_approval,
                        on_token_usage=self._record_token_usage,
                        on_thread_started=self._register_terminal_thread,
                    )
                    await self.proxy.start()
                    backoff = 1.0
                    process_done = asyncio.create_task(self.process.wait())
                    stop_done = asyncio.create_task(self.stop_event.wait())
                    done, pending = await asyncio.wait(
                        [process_done, stop_done], return_when=asyncio.FIRST_COMPLETED
                    )
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    if stop_done in done:
                        break
                    if (
                        process_done in done
                        and self.config.progress.notify_interruption
                    ):
                        await self._notify_interrupted_turns(
                            self.proxy.active_turns(),
                            process_done.result(),
                        )
                finally:
                    if self.proxy:
                        await self.proxy.close()
                        self.proxy = None
                    await self._stop_child()
                if not self.stop_event.is_set():
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
        finally:
            await self._clear_all_remote_progress()
            await self._stop_child()
            if self.registry:
                self.registry.close()
                self.registry = None
            if self.approval_store:
                self.approval_store.close()
                self.approval_store = None
            self.paths.pid_file.unlink(missing_ok=True)
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    async def _notify_terminal_completion(self, thread_id: str, text: str) -> None:
        mapping = self.registry.find_by_thread(thread_id) if self.registry else None
        if mapping is None:
            logger.warning(
                "no Feishu route for terminal completion: thread=%s", thread_id
            )
            return
        result = format_result(text) or "(无文本输出)"
        message = (
            "Codex 回合已完成\n"
            f"**会话**：{mapping.session_label} | {mapping.short_thread_id}\n"
            f"**目录**：{mapping.cwd}\n"
            f"**结果**：\n\n{result}"
        )
        route_name = self._thread_route_name(thread_id, mapping, fallback="default")
        await self._send_to_notification_route(route_name, message)

    async def _register_terminal_thread(
        self, thread_id: str, cwd: str, _origin: str
    ) -> None:
        """Persist the configured Codex route for a newly created TUI thread."""
        if self.registry is None:
            return
        if self.registry.get_thread_route(thread_id) is not None:
            return
        route_name = self.config.agent_routes.get("codex", "default")
        route = self.config.notification_routes.get(route_name)
        if route is None:
            logger.warning(
                "new terminal thread has no configured Codex route: thread=%s route=%s",
                thread_id,
                route_name,
            )
            return
        thread_cwd = cwd or os.getcwd()
        external_key = f"feishu:{route.receive_id}:terminal:{thread_id}"
        mapping = self.registry.subscribe(
            "cc_connect",
            route.project,
            external_key,
            thread_id,
            thread_cwd,
        )
        self.registry.set_thread_route(
            thread_id,
            "codex",
            route_name,
            mapping.project,
            mapping.external_key,
            mapping.cwd,
        )
        logger.info(
            "registered new terminal Codex thread route: thread=%s route=%s",
            thread_id,
            route_name,
        )

    async def _notify_interrupted_turns(
        self,
        active_turns: list[tuple[str, str]],
        returncode: int | None,
    ) -> None:
        for thread_id, origin in active_turns:
            await self._clear_remote_progress(thread_id)
            mapping = (
                self.registry.find_by_thread(thread_id)
                if self.registry
                else None
            )
            if mapping is None:
                logger.warning(
                    "no Feishu route for interrupted turn: thread=%s",
                    thread_id,
                )
                continue
            message = (
                "Codex 回合异常中断\n"
                f"会话：{mapping.session_label} | {mapping.short_thread_id}\n"
                f"目录：{mapping.cwd}\n"
                f"App Server 退出码：{returncode}\n"
                "Agent Notifier 将自动重启服务；请确认任务状态后重试。"
            )
            route_name = self._thread_route_name(
                thread_id, mapping, fallback=None
            )
            if route_name:
                await self._send_to_notification_route(route_name, message)
            elif origin == "terminal":
                await self._send_to_notification_route("default", message)
            else:
                await self._send_to_mapping(mapping, message)

    async def _record_token_usage(
        self, thread_id: str, token_usage: dict
    ) -> None:
        if self.registry:
            self.registry.update_thread_token_usage(thread_id, token_usage)

    async def _notify_remote_progress(self, thread_id: str, text: str) -> None:
        mapping = self.registry.find_by_thread(thread_id) if self.registry else None
        if mapping is None:
            logger.warning(
                "no Feishu route for remote progress: thread=%s", thread_id
            )
            return
        route_name = self._thread_route_name(thread_id, mapping, fallback=None)
        route = self.config.notification_routes.get(route_name) if route_name else None
        if route is not None and self.config.progress.moving_onit:
            try:
                message_id, reaction_id = await send_progress_message_with_onit(
                    project=route.project,
                    receive_id=route.receive_id,
                    text=text.strip(),
                )
            except Exception:
                logger.exception(
                    "session route progress send failed; falling back: thread=%s",
                    thread_id,
                )
                await self._send_to_notification_route(route_name, text.strip())
                return
            previous = self._remote_progress_onit.get(thread_id)
            self._remote_progress_onit[thread_id] = (
                route.project, message_id, reaction_id
            )
            if previous:
                try:
                    await self._remove_remote_progress_reaction(previous)
                except Exception:
                    logger.exception(
                        "failed to remove previous progress reaction: thread=%s",
                        thread_id,
                    )
            return
        if route is not None:
            await self._send_to_notification_route(route_name, text.strip())
            return
        receive_id = mapping.external_key.rsplit(":", 1)[-1]
        try:
            message_id, reaction_id = (
                await send_progress_message_with_onit(
                    project=mapping.project,
                    receive_id=receive_id,
                    text=text.strip(),
                )
            )
        except Exception:
            logger.exception(
                "moving OnIt send failed; falling back to cc-connect: "
                "thread=%s",
                thread_id,
            )
            await self._send_to_mapping(mapping, text.strip())
            return
        previous = self._remote_progress_onit.get(thread_id)
        self._remote_progress_onit[thread_id] = (
            mapping.project,
            message_id,
            reaction_id,
        )
        if previous:
            try:
                await self._remove_remote_progress_reaction(previous)
            except Exception:
                logger.exception(
                    "failed to remove previous progress reaction: "
                    "thread=%s",
                    thread_id,
                )

    async def _finish_remote_progress(
        self, thread_id: str, _text: str
    ) -> None:
        await self._clear_remote_progress(thread_id)

    async def _clear_remote_progress(self, thread_id: str) -> None:
        current = self._remote_progress_onit.pop(thread_id, None)
        if current:
            try:
                await self._remove_remote_progress_reaction(current)
            except Exception:
                logger.exception(
                    "failed to clear remote progress reaction: thread=%s",
                    thread_id,
                )

    async def _clear_all_remote_progress(self) -> None:
        current = list(self._remote_progress_onit.values())
        self._remote_progress_onit.clear()
        for reaction in current:
            try:
                await self._remove_remote_progress_reaction(reaction)
            except Exception:
                logger.exception("failed to clear remote progress reaction")

    @staticmethod
    async def _remove_remote_progress_reaction(
        reaction: tuple[str, str, str]
    ) -> None:
        project, message_id, reaction_id = reaction
        await remove_message_reaction(
            project=project,
            message_id=message_id,
            reaction_id=reaction_id,
        )

    async def _notify_approval_request(
        self, token: str, request: dict, origin: str
    ) -> None:
        params = request.get("params") or {}
        thread_id = params.get("threadId")
        mapping = (
            self.registry.find_by_thread(thread_id)
            if self.registry and thread_id
            else None
        )
        if mapping is None:
            logger.warning(
                "no Feishu route for approval: approval=%s thread=%s",
                approval_short_id(token),
                thread_id,
            )
            return
        request_id = approval_short_id(token)
        reason, operation = approval_details(request)
        target_mapping = mapping
        receive_id_type = "open_id"
        receive_id = mapping.external_key.rsplit(":", 1)[-1]
        target_project = mapping.project
        route_name = self._thread_route_name(thread_id, mapping, fallback=None)
        route = (
            self.config.notification_routes.get(route_name)
            if route_name else None
        )
        if route is None and origin == "terminal":
            route_name = "default"
            route = self.config.notification_routes.get(route_name)
        if route is not None:
            target_project = route.project
            receive_id = route.receive_id
            receive_id_type = route.receive_id_type
        try:
            message_id = await send_approval_card(
                project=target_project,
                receive_id=receive_id,
                receive_id_type=receive_id_type,
                session_key=(
                    route.session_key or target_mapping.external_key
                    if route
                    else target_mapping.external_key
                ),
                approval_id=request_id,
                reason=reason,
                operation=operation,
                session_label=mapping.session_label,
                thread_id=thread_id,
                cwd=mapping.cwd,
            )
            if message_id and self.approval_store:
                self.approval_store.set_feishu_message_id(token, message_id)
        except Exception:
            logger.exception(
                "Feishu approval card failed; falling back to text: approval=%s",
                request_id,
            )
            if route is not None:
                await self._send_to_notification_route(
                    route_name, format_approval_message(token, request)
                )
            else:
                await self._send_to_mapping(
                    target_mapping, format_approval_message(token, request)
                )

    async def _notify_terminal_approval(self, token: str, decision: str) -> None:
        """Turn a terminal-first approval into a grey card in Feishu."""
        if self.approval_store is None or self.registry is None:
            return
        record = self.approval_store.find_by_prefix(approval_short_id(token))
        if record is None or not record.feishu_message_id:
            return
        mapping = self.registry.find_by_thread(record.thread_id)
        if mapping is None:
            return
        reason, operation = approval_details(record.payload)
        route_name = self._thread_route_name(record.thread_id, mapping, fallback=None)
        route = (
            self.config.notification_routes.get(route_name)
            if route_name else None
        )
        await reply_approval_result_card(
            project=route.project if route else mapping.project,
            message_id=record.feishu_message_id,
            approval_id=approval_short_id(token),
            decision=decision,
            status="already_resolved",
            session_label=mapping.session_label,
            thread_id=record.thread_id,
            cwd=mapping.cwd,
            reason=reason,
            operation=operation,
        )

    def _notification_route(self, name: str):
        route = self.config.notification_routes.get(name)
        if route is None:
            raise RuntimeError(
                f"notification route {name!r} is not configured"
            )
        return route

    def _thread_route_name(self, thread_id: str, mapping, *, fallback: str | None):
        return resolve_thread_route_name(
            self.config, self.registry, thread_id, mapping, fallback=fallback
        )

    async def _send_to_notification_route(
        self, name: str, message: str
    ) -> None:
        route = self._notification_route(name)
        sender = (
            send_markdown_message
            if route.message_format == "markdown"
            else send_text_message
        )
        await sender(
            project=route.project,
            receive_id=route.receive_id,
            receive_id_type=route.receive_id_type,
            text=message,
        )

    async def _send_to_mapping(self, mapping, message: str) -> None:
        direct = Path.home() / ".npm-global/lib/node_modules/cc-connect/bin/cc-connect"
        binary = str(direct) if direct.exists() else shutil.which("cc-connect")
        if not binary:
            raise RuntimeError("cc-connect executable not found")
        process = await asyncio.create_subprocess_exec(
            binary,
            "send",
            "--stdin",
            "-p",
            mapping.project,
            "-s",
            mapping.external_key,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate(message.encode())
        if process.returncode:
            raise RuntimeError(
                f"cc-connect send failed ({process.returncode}): "
                f"{stderr.decode(errors='replace').strip()}"
            )

    async def _stop_child(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None
