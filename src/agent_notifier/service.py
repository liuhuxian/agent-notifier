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
from .config import Paths
from .feishu import send_approval_card
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
        self.stop_event = asyncio.Event()
        self.process: asyncio.subprocess.Process | None = None
        self.proxy: AppServerProxy | None = None
        self.registry: SessionRegistry | None = None
        self.approval_store: ApprovalStore | None = None

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
                        approval_store=self.approval_store,
                        on_approval_request=self._notify_approval_request,
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
                finally:
                    if self.proxy:
                        await self.proxy.close()
                        self.proxy = None
                    await self._stop_child()
                if not self.stop_event.is_set():
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
        finally:
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
        result = text.strip() or "(无文本输出)"
        message = (
            "Codex 回合已完成\n"
            f"会话：{mapping.session_label} | {mapping.short_thread_id}\n"
            f"目录：{mapping.cwd}\n"
            f"结果：\n{result}"
        )
        await self._send_to_mapping(mapping, message)

    async def _notify_approval_request(self, token: str, request: dict) -> None:
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
        receive_id = mapping.external_key.rsplit(":", 1)[-1]
        try:
            message_id = await send_approval_card(
                project=mapping.project,
                receive_id=receive_id,
                session_key=mapping.external_key,
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
            await self._send_to_mapping(
                mapping, format_approval_message(token, request)
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
