"""Shared Codex App Server and proxy process supervision."""

from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import stat
import shutil
from pathlib import Path

from .approvals import ApprovalStore
from .config import Paths
from .proxy import AppServerProxy
from .registry import SessionRegistry


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
                        self._notify_terminal_completion,
                        self.approval_store,
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
            return
        direct = Path.home() / ".npm-global/lib/node_modules/cc-connect/bin/cc-connect"
        binary = str(direct) if direct.exists() else shutil.which("cc-connect")
        if not binary:
            return
        message = text.strip() or f"Codex turn completed: {thread_id}"
        process = await asyncio.create_subprocess_exec(
            binary,
            "send",
            "--stdin",
            "-p",
            mapping.project,
            "-s",
            mapping.external_key,
            stdin=asyncio.subprocess.PIPE,
        )
        await process.communicate(message.encode())

    async def _stop_child(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None
