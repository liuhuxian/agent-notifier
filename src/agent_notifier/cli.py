"""Command-line interface for Agent Notifier."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

from aiohttp import ClientSession, UnixConnector

from .approvals import ApprovalStore
from .acp.server import ACPStdioServer
from .cc_config import configure_project
from .codex.backend import CodexBackend
from .codex.client import CodexAppServerClient
from .config import Paths
from .feishu import reply_approval_result_card
from .hook_config import decode_command, default_hooks_path, gate_hooks
from .registry import SessionRegistry
from .service import SharedService
from .versioning import (
    CompatibilityError,
    parse_cc_connect_version,
    parse_codex_version,
    require_supported_version,
)


def _resume_thread_id(codex_args: list[str]) -> str | None:
    try:
        index = codex_args.index("resume")
    except ValueError:
        return None
    if index + 1 >= len(codex_args):
        return None
    candidate = codex_args[index + 1]
    return candidate if candidate and not candidate.startswith("-") else None


def bind_terminal_thread(
    paths: Paths,
    thread_id: str,
    project: str | None = None,
    cwd: str | None = None,
):
    registry = SessionRegistry(paths.state_db)
    try:
        routes = registry.list_routes("cc_connect", project)
        scope = f"project {project!r}" if project else "all configured projects"
        if not routes:
            raise ValueError(f"no Feishu route is registered for {scope}")
        if len(routes) > 1:
            projects = ", ".join(sorted({route.project for route in routes}))
            raise ValueError(
                f"multiple Feishu routes are registered for {scope} "
                f"(projects: {projects}); specify --notify-project"
            )
        route = routes[0]
        return registry.subscribe(
            route.adapter,
            route.project,
            route.external_key,
            thread_id,
            cwd or route.cwd,
        )
    finally:
        registry.close()


def activate_terminal_thread(
    paths: Paths,
    thread_id: str,
    project: str | None = None,
):
    """Select one subscribed thread as the target for incoming chat tasks."""
    registry = SessionRegistry(paths.state_db)
    try:
        subscriptions = registry.list_subscriptions_by_thread(
            thread_id, project=project
        )
        if not subscriptions:
            scope = f" in project {project!r}" if project else ""
            raise ValueError(
                f"thread {thread_id!r} has no Feishu notification subscription{scope}"
            )
        if len(subscriptions) > 1:
            projects = ", ".join(
                sorted({subscription.project for subscription in subscriptions})
            )
            raise ValueError(
                f"thread {thread_id!r} has multiple Feishu subscriptions "
                f"(projects: {projects}); specify --project"
            )
        route = subscriptions[0]
        return registry.bind(
            route.adapter,
            route.project,
            route.external_key,
            route.thread_id,
            route.cwd,
        )
    finally:
        registry.close()


async def decide_approval(paths: Paths, decision: str, approval_id: str) -> str:
    connector = UnixConnector(path=str(paths.proxy_socket))
    async with ClientSession(connector=connector) as session:
        async with session.post(
            "http://localhost/approval",
            json={"decision": decision, "approval_id": approval_id},
        ) as response:
            payload = await response.json()
            if response.status >= 400:
                raise ValueError(payload.get("error") or "approval decision failed")
            status = str(payload.get("status") or "resolved")
    await reply_approval_decision(paths, decision, approval_id, status)
    return status


async def reply_approval_decision(
    paths: Paths,
    decision: str,
    approval_id: str,
    status: str,
) -> None:
    store = ApprovalStore(paths.state_db)
    try:
        record = store.find_by_prefix(approval_id)
    finally:
        store.close()
    if record is None:
        raise ValueError(f"approval record not found: {approval_id}")
    if not record.feishu_message_id:
        raise ValueError(f"approval has no Feishu message id: {approval_id}")
    registry = SessionRegistry(paths.state_db)
    try:
        mapping = registry.find_by_thread(record.thread_id)
    finally:
        registry.close()
    if mapping is None:
        raise ValueError(f"approval thread has no Feishu route: {record.thread_id}")
    await reply_approval_result_card(
        project=mapping.project,
        message_id=record.feishu_message_id,
        approval_id=approval_id,
        decision=decision,
        status=status,
    )


def approval_decision_message(
    status: str, decision: str, approval_id: str, quiet: bool
) -> str | None:
    if quiet:
        return None
    if status == "already_resolved":
        return (
            "<text_tag color='orange'>**⚠️ 审批已经被其他端处理**</text_tag>\n\n"
            f"请求 ID：`{approval_id}`"
        )
    if decision == "allow":
        return (
            "<text_tag color='green'>**✅ 已允许 Codex 权限请求**</text_tag>\n\n"
            f"请求 ID：`{approval_id}`"
        )
    return (
        "<text_tag color='red'>**❌ 已拒绝 Codex 权限请求**</text_tag>\n\n"
        f"请求 ID：`{approval_id}`"
    )


def _run_version(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"failed: {' '.join(command)}")
    return result.stdout + result.stderr


def _cc_connect_binary() -> str:
    configured = os.environ.get("AGENT_NOTIFIER_CC_CONNECT")
    if configured:
        path = Path(configured).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"configured cc-connect does not exist: {path}")
        return str(path)
    direct = Path.home() / ".npm-global/lib/node_modules/cc-connect/bin/cc-connect"
    if direct.exists():
        return str(direct)
    found = shutil.which("cc-connect")
    if found:
        return found
    raise FileNotFoundError("cc-connect is not installed")


def check_versions(
    require_cc: bool = True, codex_binary: str = "codex"
) -> tuple[str, str | None]:
    codex_version = parse_codex_version(_run_version([codex_binary, "--version"]))
    require_supported_version("codex", codex_version)
    cc_version = None
    if require_cc:
        cc_version = parse_cc_connect_version(
            _run_version([_cc_connect_binary(), "--version"])
        )
        require_supported_version("cc-connect", cc_version)
    return codex_version, cc_version


def _socket_ready(path: Path) -> bool:
    try:
        if not stat.S_ISSOCK(path.stat().st_mode):
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(str(path))
        return True
    except (FileNotFoundError, ConnectionError, OSError):
        return False


def _wait_for_service(paths: Paths, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _socket_ready(paths.proxy_socket):
            return True
        time.sleep(0.1)
    return False


def ensure_service(paths: Paths, timeout: float = 20) -> None:
    if _socket_ready(paths.proxy_socket):
        return
    systemctl = shutil.which("systemctl")
    if systemctl:
        started = subprocess.run(
            [systemctl, "--user", "start", "agent-notifier.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
        if started and _wait_for_service(paths, min(timeout, 10)):
            return
    if not _socket_ready(paths.proxy_socket):
        paths.ensure_directories()
        log_path = paths.log_dir / "service.log"
        log = log_path.open("a")
        subprocess.Popen(
            [sys.executable, "-m", "agent_notifier.cli", "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        log.close()
    if _wait_for_service(paths, timeout):
        return
    raise TimeoutError(f"agent-notifier service did not start; see {paths.log_dir}")


async def run_acp(paths: Paths) -> None:
    ensure_service(paths)
    rpc = CodexAppServerClient(paths.proxy_socket, "cc_connect")
    await rpc.connect()
    registry = SessionRegistry(paths.state_db)
    backend = CodexBackend(rpc, registry)
    server = ACPStdioServer(backend, sys.stdin, sys.stdout)
    rpc.set_server_request_handler(server.handle_codex_request)
    try:
        await server.run()
    finally:
        await backend.close()
        registry.close()
        await rpc.close()


def configure_cc(paths: Paths, project: str, config_path: Path) -> Path:
    paths.ensure_directories()
    source = config_path.read_text()
    command = shutil.which("agent-notifier") or str(Path(sys.argv[0]).resolve())
    updated = configure_project(source, project, command)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup = config_path.with_name(f"{config_path.name}.agent-notifier.{timestamp}.bak")
    shutil.copy2(config_path, backup)
    temporary = config_path.with_suffix(config_path.suffix + ".agent-notifier.tmp")
    temporary.write_text(updated)
    os.replace(temporary, config_path)
    (paths.config_dir / "last_cc_connect_backup").write_text(str(backup))
    return backup


def restore_cc(paths: Paths, config_path: Path) -> Path:
    marker = paths.config_dir / "last_cc_connect_backup"
    if not marker.exists():
        raise FileNotFoundError("no cc-connect backup recorded")
    backup = Path(marker.read_text().strip())
    if not backup.exists():
        raise FileNotFoundError(f"backup no longer exists: {backup}")
    shutil.copy2(backup, config_path)
    return backup


def configure_hooks(paths: Paths, hooks_path: Path) -> Path | None:
    if not hooks_path.exists():
        return None
    paths.ensure_directories()
    executable = shutil.which("agent-notifier") or str(Path(sys.argv[0]).resolve())
    updated, changed = gate_hooks(hooks_path.read_text(), executable)
    if changed == 0:
        return None
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup = hooks_path.with_name(f"{hooks_path.name}.agent-notifier.{timestamp}.bak")
    shutil.copy2(hooks_path, backup)
    temporary = hooks_path.with_suffix(hooks_path.suffix + ".agent-notifier.tmp")
    temporary.write_text(updated)
    os.replace(temporary, hooks_path)
    (paths.config_dir / "last_codex_hooks_backup").write_text(str(backup))
    return backup


def restore_hooks(paths: Paths, hooks_path: Path) -> Path:
    marker = paths.config_dir / "last_codex_hooks_backup"
    if not marker.exists():
        raise FileNotFoundError("no Codex hooks backup recorded")
    backup = Path(marker.read_text().strip())
    if not backup.exists():
        raise FileNotFoundError(f"backup no longer exists: {backup}")
    shutil.copy2(backup, hooks_path)
    return backup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-notifier")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the shared background service")
    serve.add_argument(
        "--codex",
        default=os.environ.get("AGENT_NOTIFIER_CODEX", "codex"),
        help="Codex CLI executable used for the managed App Server",
    )
    sub.add_parser("acp", help="run the cc-connect ACP stdio adapter")
    codex = sub.add_parser("codex", help="launch native Codex TUI through the service")
    codex.add_argument(
        "--notify-project",
        help="Feishu project to bind when resuming a Codex thread",
    )
    codex.add_argument("codex_args", nargs=argparse.REMAINDER)
    bind = sub.add_parser(
        "bind", help="bind an existing Codex thread to a Feishu project"
    )
    bind.add_argument("thread_id")
    bind.add_argument("--project", required=True)
    activate = sub.add_parser(
        "activate", help="select a subscribed Codex thread for incoming chat tasks"
    )
    activate.add_argument("thread_id")
    activate.add_argument("--project")
    decide = sub.add_parser(
        "decide", help="resolve a pending Codex approval request"
    )
    decide.add_argument(
        "--quiet",
        action="store_true",
        help="suppress success output for interactive card callbacks",
    )
    decide.add_argument("decision", choices=("allow", "deny"))
    decide.add_argument("approval_id")
    sub.add_parser("status", help="show service status")
    sub.add_parser("doctor", help="validate versions, paths, and service")
    logs = sub.add_parser("logs", help="follow service logs")
    logs.add_argument("--no-follow", action="store_true")
    configure = sub.add_parser("configure-cc", help="configure one cc-connect project")
    configure.add_argument("--project", required=True)
    configure.add_argument("--config", type=Path, default=Path.home() / ".cc-connect/config.toml")
    restore = sub.add_parser("restore-cc", help="restore the last cc-connect backup")
    restore.add_argument("--config", type=Path, default=Path.home() / ".cc-connect/config.toml")
    hooks = sub.add_parser("configure-hooks", help="gate duplicate Codex notification hooks")
    hooks.add_argument("--hooks", type=Path, default=default_hooks_path())
    restore_hooks_parser = sub.add_parser(
        "restore-hooks", help="restore the last Codex hooks backup"
    )
    restore_hooks_parser.add_argument("--hooks", type=Path, default=default_hooks_path())
    gate = sub.add_parser("hook-gate", help=argparse.SUPPRESS)
    gate.add_argument("--encoded", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    paths = Paths.from_environment()
    try:
        if args.command == "serve":
            check_versions(require_cc=False, codex_binary=args.codex)
            asyncio.run(SharedService(paths, codex_binary=args.codex).run())
        elif args.command == "acp":
            check_versions(require_cc=True)
            asyncio.run(run_acp(paths))
        elif args.command == "codex":
            check_versions(require_cc=False)
            ensure_service(paths)
            thread_id = _resume_thread_id(args.codex_args)
            if args.notify_project and thread_id is None:
                raise ValueError(
                    "--notify-project requires `agent-notifier codex "
                    "[--notify-project PROJECT] resume THREAD_ID`"
                )
            if thread_id:
                mapping = bind_terminal_thread(
                    paths,
                    thread_id,
                    project=args.notify_project,
                    cwd=os.getcwd(),
                )
                print(
                    f"Feishu notifications: {mapping.project} "
                    f"<- {mapping.thread_id}",
                    file=sys.stderr,
                )
            os.execvp(
                "codex",
                ["codex", "--remote", f"unix://{paths.proxy_socket}", *args.codex_args],
            )
        elif args.command == "bind":
            mapping = bind_terminal_thread(
                paths,
                args.thread_id,
                project=args.project,
                cwd=os.getcwd(),
            )
            print(f"bound: {mapping.project} <- {mapping.thread_id}")
        elif args.command == "activate":
            mapping = activate_terminal_thread(
                paths,
                args.thread_id,
                project=args.project,
            )
            print(f"active: {mapping.project} -> {mapping.thread_id}")
        elif args.command == "decide":
            ensure_service(paths)
            status = asyncio.run(
                decide_approval(paths, args.decision, args.approval_id)
            )
            message = approval_decision_message(
                status, args.decision, args.approval_id, args.quiet
            )
            if message:
                print(message)
        elif args.command == "status":
            running = _socket_ready(paths.proxy_socket)
            pid = paths.pid_file.read_text().strip() if paths.pid_file.exists() else "-"
            print(f"status: {'running' if running else 'stopped'}")
            print(f"pid: {pid}")
            print(f"socket: {paths.proxy_socket}")
        elif args.command == "doctor":
            codex_version, cc_version = check_versions(require_cc=True)
            print(f"Codex CLI: {codex_version} (supported)")
            print(f"cc-connect: {cc_version} (supported)")
            print(f"state: {paths.state_dir}")
            print(f"runtime: {paths.runtime_dir}")
            print(f"service: {'running' if _socket_ready(paths.proxy_socket) else 'stopped'}")
        elif args.command == "logs":
            journalctl = shutil.which("journalctl")
            if journalctl:
                command = [journalctl, "--user", "-u", "agent-notifier.service", "-n", "100"]
                if not args.no_follow:
                    command.append("-f")
                os.execvp(journalctl, command)
            log = paths.log_dir / "service.log"
            command = ["tail", "-n", "100"]
            if not args.no_follow:
                command.append("-f")
            command.append(str(log))
            os.execvp("tail", command)
        elif args.command == "configure-cc":
            backup = configure_cc(paths, args.project, args.config)
            print(f"configured: {args.config}")
            print(f"backup: {backup}")
        elif args.command == "restore-cc":
            backup = restore_cc(paths, args.config)
            print(f"restored from: {backup}")
        elif args.command == "configure-hooks":
            backup = configure_hooks(paths, args.hooks)
            if backup:
                print(f"configured: {args.hooks}")
                print(f"backup: {backup}")
            else:
                print("no ungated Stop/PermissionRequest hooks found")
        elif args.command == "restore-hooks":
            backup = restore_hooks(paths, args.hooks)
            print(f"restored from: {backup}")
        elif args.command == "hook-gate":
            if os.environ.get("AGENT_NOTIFIER_MANAGED") == "1":
                return
            command = decode_command(args.encoded)
            raise SystemExit(subprocess.call(["bash", "-lc", command]))
    except (CompatibilityError, FileNotFoundError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
