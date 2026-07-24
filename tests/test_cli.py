import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_notifier.approvals import ApprovalStore
from agent_notifier.agent_commands import format_agent_help, run_agent_command
from agent_notifier.cli import (
    _resume_thread_id,
    activate_terminal_thread,
    approval_decision_message,
    bind_terminal_thread,
    build_parser,
    create_agent_session,
    current_agent_sessions,
    format_agent_new,
    format_agent_switch,
    list_agent_sessions,
    reply_approval_decision,
    send_configured_notification,
    switch_agent_session,
)
from agent_notifier.config import Paths
from agent_notifier.registry import SessionRegistry


def make_paths(root: Path) -> Paths:
    return Paths(
        config_dir=root / "config",
        state_dir=root / "state",
        runtime_dir=root / "run",
        log_dir=root / "state/logs",
        proxy_socket=root / "run/proxy.sock",
        upstream_socket=root / "run/upstream.sock",
        state_db=root / "state/state.sqlite3",
        pid_file=root / "run/service.pid",
        lock_file=root / "run/service.lock",
    )


class CodexBindingTest(unittest.TestCase):
    def test_resume_thread_is_extracted_from_passthrough_args(self):
        self.assertEqual(
            "thread-1", _resume_thread_id(["--no-alt-screen", "resume", "thread-1"])
        )
        self.assertIsNone(_resume_thread_id(["--no-alt-screen"]))

    def test_codex_parser_keeps_notifier_option_out_of_passthrough_args(self):
        args = build_parser().parse_args(
            ["codex", "--notify-project", "le-wm", "resume", "thread-1"]
        )
        self.assertEqual("le-wm", args.notify_project)
        self.assertEqual(["resume", "thread-1"], args.codex_args)

    def test_decide_parser_accepts_remote_approval(self):
        args = build_parser().parse_args(
            ["decide", "--quiet", "allow", "1234567890"]
        )
        self.assertEqual("allow", args.decision)
        self.assertEqual("1234567890", args.approval_id)
        self.assertTrue(args.quiet)

    def test_activate_parser_accepts_thread_and_optional_project(self):
        args = build_parser().parse_args(
            ["activate", "thread-1", "--project", "le-wm"]
        )
        self.assertEqual("thread-1", args.thread_id)
        self.assertEqual("le-wm", args.project)

    def test_agent_command_parsers(self):
        listed = build_parser().parse_args(["agent-list", "codex"])
        current = build_parser().parse_args(["agent-current"])
        created = build_parser().parse_args(["agent-new", "codex"])
        switched = build_parser().parse_args(
            ["agent-switch", "codex", "019e81c0"]
        )
        queried = build_parser().parse_args(["agent-cmd", "status"])
        help_command = build_parser().parse_args(["agent-help"])

        self.assertEqual("codex", listed.provider)
        self.assertEqual("agent-current", current.command)
        self.assertEqual("codex", created.provider)
        self.assertEqual("codex", switched.provider)
        self.assertEqual("019e81c0", switched.session_id)
        self.assertEqual("status", queried.agent_command)
        self.assertEqual("agent-help", help_command.command)

    def test_notify_parser_accepts_named_route_and_stdin(self):
        args = build_parser().parse_args(
            ["notify", "--route", "default", "--stdin"]
        )
        self.assertEqual("default", args.route)
        self.assertTrue(args.stdin)

    def test_configured_notification_uses_named_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            paths.config_file.write_text(
                "[notification_routes.default]\n"
                'project = "le-wm-codex"\n'
                'receive_id_type = "chat_id"\n'
                'receive_id = "oc_notify"\n'
            )
            with patch(
                "agent_notifier.cli.send_text_message",
                new=AsyncMock(return_value="om_notify"),
            ) as send_message:
                message_id = asyncio.run(
                    send_configured_notification(
                        paths, "default", "pipeline done"
                    )
                )

        self.assertEqual("om_notify", message_id)
        send_message.assert_awaited_once_with(
            project="le-wm-codex",
            receive_id="oc_notify",
            receive_id_type="chat_id",
            text="pipeline done",
        )

    def test_agent_help_lists_registered_user_commands(self):
        result = format_agent_help()
        for command in (
            "/agent-list codex",
            "/agent-current",
            "/agent-new codex",
            "/agent-switch codex",
            "/agent-cmd status",
            "/agent-cmd model",
            "/agent-cmd usage",
            "/agent-cmd session",
            "/codex-approve",
            "/codex-deny",
            "/agent-help",
        ):
            self.assertIn(command, result)
        for description in (
            "查看已订阅的 Codex 会话",
            "查看当前飞书聊天绑定的 Agent 会话",
            "新建 Codex 会话并自动切换过去",
            "切换当前飞书聊天使用的 Codex 会话",
        ):
            self.assertIn(description, result)

    def test_agent_command_rejects_non_whitelisted_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            with self.assertRaisesRegex(ValueError, "仅支持"):
                asyncio.run(
                    run_agent_command(
                        paths, "shell", "le-wm", "feishu:one"
                    )
                )

    def test_agent_commands_query_active_codex_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            registry = SessionRegistry(paths.state_db)
            registry.bind(
                "cc_connect",
                "le-wm",
                "feishu:one",
                "019f-thread",
                "/workspace/le-wm",
            )
            registry.set_active_agent("le-wm", "feishu:one", "codex")
            registry.update_thread_token_usage(
                "019f-thread",
                {
                    "total": {
                        "totalTokens": 1200,
                        "inputTokens": 1000,
                        "outputTokens": 200,
                    },
                    "last": {
                        "totalTokens": 120,
                        "inputTokens": 100,
                        "outputTokens": 20,
                    },
                    "modelContextWindow": 200000,
                },
            )
            registry.close()

            rpc = AsyncMock()
            rpc.call.side_effect = [
                {
                    "thread": {
                        "id": "019f-thread",
                        "cwd": "/workspace/le-wm",
                        "status": {"type": "idle"},
                    }
                },
                {
                    "config": {
                        "model": "gpt-5.3-codex",
                        "model_reasoning_effort": "high",
                    }
                },
                {
                    "rateLimits": {
                        "primary": {
                            "usedPercent": 12,
                            "resetsAt": 1780000000,
                        }
                    }
                },
            ]
            with patch(
                "agent_notifier.agent_commands.CodexAppServerClient",
                return_value=rpc,
            ):
                result = asyncio.run(
                    run_agent_command(
                        paths, "status", "le-wm", "feishu:one"
                    )
                )

            self.assertIn("会话：019f-thr", result)
            self.assertIn("状态：idle", result)
            self.assertIn("模型：gpt-5.3-codex", result)
            self.assertIn("累计 Token：1,200", result)
            self.assertIn("主窗口：已用 12%", result)
            rpc.connect.assert_awaited_once()
            rpc.close.assert_awaited_once()

    def test_agent_new_creates_and_activates_codex_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            registry = SessionRegistry(paths.state_db)
            registry.bind(
                "cc_connect",
                "le-wm-codex",
                "feishu:one",
                "thread-old",
                "/workspace/le-wm",
            )
            registry.close()

            rpc = AsyncMock()
            rpc.call.side_effect = [
                {"thread": {"id": "019f-new-thread"}},
                {"turn": {"id": "turn-bootstrap"}},
            ]
            rpc.next_event.side_effect = [
                {
                    "method": "turn/started",
                    "params": {
                        "threadId": "019f-new-thread",
                        "turn": {"id": "turn-bootstrap"},
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "019f-new-thread",
                        "turn": {
                            "id": "turn-bootstrap",
                            "status": "completed",
                        },
                    },
                },
            ]
            with patch(
                "agent_notifier.cli.CodexAppServerClient",
                return_value=rpc,
            ):
                mapping = asyncio.run(
                    create_agent_session(
                        paths,
                        "codex",
                        project="le-wm-codex",
                        external_key="feishu:one",
                    )
                )

            rpc.connect.assert_awaited_once()
            self.assertEqual(2, rpc.call.await_count)
            rpc.call.assert_any_await(
                "thread/start",
                {
                    "cwd": "/workspace/le-wm",
                    "approvalPolicy": "on-request",
                    "threadSource": "user",
                },
            )
            rpc.call.assert_any_await(
                "turn/start",
                {
                    "threadId": "019f-new-thread",
                    "input": [
                        {
                            "type": "text",
                            "text": "初始化会话，只回复 OK。",
                            "text_elements": [],
                        }
                    ],
                },
            )
            self.assertEqual(2, rpc.next_event.await_count)
            rpc.close.assert_awaited_once()
            self.assertEqual("019f-new-thread", mapping.thread_id)
            registry = SessionRegistry(paths.state_db)
            self.assertEqual(
                "019f-new-thread",
                registry.get(
                    "cc_connect", "le-wm-codex", "feishu:one"
                ).thread_id,
            )
            registry.close()

    def test_agent_new_result_is_feishu_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            registry = SessionRegistry(paths.state_db)
            mapping = registry.bind(
                "cc_connect",
                "le-wm-codex",
                "feishu:one",
                "019f8dfd-130a-7a92-a115-f437e38e40a8",
                "/workspace/le-wm",
            )
            registry.close()

            self.assertEqual(
                "Agent 会话已新建\n\n"
                "类型：codex\n"
                "会话：019f8dfd\n"
                "目录：/workspace/le-wm",
                format_agent_new("codex", mapping),
            )

    def test_agent_new_failure_keeps_current_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            registry = SessionRegistry(paths.state_db)
            registry.bind(
                "cc_connect",
                "le-wm-codex",
                "feishu:one",
                "thread-old",
                "/workspace/le-wm",
            )
            registry.close()

            rpc = AsyncMock()
            rpc.call.side_effect = RuntimeError("thread creation failed")
            with patch(
                "agent_notifier.cli.CodexAppServerClient",
                return_value=rpc,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "thread creation failed"
                ):
                    asyncio.run(
                        create_agent_session(
                            paths,
                            "codex",
                            project="le-wm-codex",
                            external_key="feishu:one",
                        )
                    )

            rpc.close.assert_awaited_once()
            registry = SessionRegistry(paths.state_db)
            self.assertEqual(
                "thread-old",
                registry.get(
                    "cc_connect", "le-wm-codex", "feishu:one"
                ).thread_id,
            )
            registry.close()

    def test_agent_new_bootstrap_failure_keeps_current_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            registry = SessionRegistry(paths.state_db)
            registry.bind(
                "cc_connect",
                "le-wm-codex",
                "feishu:one",
                "thread-old",
                "/workspace/le-wm",
            )
            registry.close()

            rpc = AsyncMock()
            rpc.call.side_effect = [
                {"thread": {"id": "019f-new-thread"}},
                {"turn": {"id": "turn-bootstrap"}},
            ]
            rpc.next_event.return_value = {
                "method": "turn/completed",
                "params": {
                    "threadId": "019f-new-thread",
                    "turn": {
                        "id": "turn-bootstrap",
                        "status": "failed",
                        "error": {"message": "bootstrap failed"},
                    },
                },
            }
            with patch(
                "agent_notifier.cli.CodexAppServerClient",
                return_value=rpc,
            ):
                with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
                    asyncio.run(
                        create_agent_session(
                            paths,
                            "codex",
                            project="le-wm-codex",
                            external_key="feishu:one",
                        )
                    )

            rpc.close.assert_awaited_once()
            registry = SessionRegistry(paths.state_db)
            self.assertEqual(
                "thread-old",
                registry.get(
                    "cc_connect", "le-wm-codex", "feishu:one"
                ).thread_id,
            )
            registry.close()

    def test_agent_switch_result_is_feishu_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            registry = SessionRegistry(paths.state_db)
            mapping = registry.bind(
                "cc_connect",
                "le-wm-codex",
                "feishu:one",
                "019e81c0-c415-7820-8921-2b32f1bee990",
                "/workspace/le-wm",
            )
            registry.close()

            self.assertEqual(
                "Agent 会话已切换\n\n"
                "类型：codex\n"
                "会话：019e81c0\n"
                "目录：/workspace/le-wm",
                format_agent_switch("codex", mapping),
            )

    def test_quiet_card_callback_reports_already_handled_approval(self):
        message = approval_decision_message(
            "already_resolved", "allow", "1234567890", quiet=True
        )
        self.assertIsNone(message)

    def test_quiet_card_callback_suppresses_normal_success(self):
        self.assertIsNone(
            approval_decision_message(
                "resolved", "allow", "1234567890", quiet=True
            )
        )

    def test_nonquiet_result_is_compact_local_time(self):
        message = approval_decision_message(
            "resolved",
            "allow",
            "1234567890",
            quiet=False,
            now=datetime(2026, 7, 23, 15, 30),
        )
        self.assertEqual("2026-07-23 15:30", message)

    def test_terminal_resume_adds_subscription_without_changing_active_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            registry = SessionRegistry(paths.state_db)
            registry.bind(
                "cc_connect", "le-wm", "feishu:one", "thread-old", "/workspace"
            )
            registry.close()

            mapping = bind_terminal_thread(
                paths, "thread-new", project="le-wm", cwd="/new-workspace"
            )
            self.assertEqual("thread-new", mapping.thread_id)
            self.assertEqual("/new-workspace", mapping.cwd)

            registry = SessionRegistry(paths.state_db)
            self.assertEqual(
                "thread-old",
                registry.get("cc_connect", "le-wm", "feishu:one").thread_id,
            )
            self.assertEqual(
                "feishu:one",
                registry.find_by_thread("thread-old").external_key,
            )
            self.assertEqual(
                "feishu:one",
                registry.find_by_thread("thread-new").external_key,
            )
            registry.close()

    def test_terminal_resume_prefers_default_notification_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            paths.config_file.write_text(
                "[notification_routes.default]\n"
                'project = "le-wm-codex"\n'
                'receive_id_type = "chat_id"\n'
                'receive_id = "oc_notify"\n'
            )
            registry = SessionRegistry(paths.state_db)
            registry.bind(
                "cc_connect",
                "le-wm-codex",
                "feishu:oc_interactive:ou_user",
                "thread-interactive",
                "/interactive",
            )
            registry.bind(
                "cc_connect",
                "le-wm-codex",
                "feishu:oc_notify:ou_user",
                "thread-notify",
                "/notify",
            )
            registry.close()

            mapping = bind_terminal_thread(
                paths,
                "thread-terminal",
                project="le-wm-codex",
                cwd="/terminal",
            )

        self.assertEqual(
            "feishu:oc_notify:terminal:thread-terminal",
            mapping.external_key,
        )
        self.assertEqual("thread-terminal", mapping.thread_id)

    def test_terminal_resume_uses_unregistered_notification_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            paths.config_file.write_text(
                "[notification_routes.default]\n"
                'project = "le-wm-codex"\n'
                'receive_id_type = "chat_id"\n'
                'receive_id = "oc_notify"\n'
            )

            mapping = bind_terminal_thread(
                paths,
                "thread-terminal",
                project="le-wm-codex",
                cwd="/terminal",
            )

        self.assertEqual(
            "feishu:oc_notify:terminal:thread-terminal",
            mapping.external_key,
        )
        self.assertEqual("thread-terminal", mapping.thread_id)

    def test_ambiguous_routes_require_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            registry = SessionRegistry(paths.state_db)
            registry.bind("cc_connect", "one", "feishu:one", "thread-1", "/one")
            registry.bind("cc_connect", "two", "feishu:two", "thread-2", "/two")
            registry.close()

            with self.assertRaisesRegex(ValueError, "specify --notify-project"):
                bind_terminal_thread(paths, "thread-new")

    def test_activate_changes_task_target_without_removing_subscriptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            registry = SessionRegistry(paths.state_db)
            registry.bind(
                "cc_connect", "le-wm", "feishu:one", "thread-old", "/old"
            )
            registry.subscribe(
                "cc_connect", "le-wm", "feishu:one", "thread-new", "/new"
            )
            registry.close()

            active = activate_terminal_thread(
                paths, "thread-new", project="le-wm"
            )
            self.assertEqual("thread-new", active.thread_id)

            registry = SessionRegistry(paths.state_db)
            self.assertEqual(
                "thread-new",
                registry.get("cc_connect", "le-wm", "feishu:one").thread_id,
            )
            self.assertEqual(
                {"thread-old", "thread-new"},
                {
                    route.thread_id
                    for route in registry.list_subscriptions(
                        "cc_connect", "le-wm", "feishu:one"
                    )
                },
            )
            registry.close()

    def test_agent_list_current_and_short_id_switch_are_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = make_paths(root)
            paths.ensure_directories()
            codex_home = root / "codex"
            sessions_dir = codex_home / "sessions/2026/07/23"
            sessions_dir.mkdir(parents=True)
            for thread_id in (
                "019f8dfd-130a-7a92-a115-f437e38e40a8",
                "019e81c0-c415-7820-8921-2b32f1bee990",
            ):
                (
                    sessions_dir
                    / f"rollout-2026-07-23T10-00-00-{thread_id}.jsonl"
                ).write_text("{}\n")
            registry = SessionRegistry(paths.state_db)
            registry.bind(
                "cc_connect",
                "le-wm-codex",
                "feishu:one",
                "019f8dfd-130a-7a92-a115-f437e38e40a8",
                "/workspace/old",
            )
            registry.subscribe(
                "cc_connect",
                "le-wm-codex",
                "feishu:one",
                "019e81c0-c415-7820-8921-2b32f1bee990",
                "/workspace/le-wm",
            )
            registry.bind(
                "cc_connect",
                "other",
                "feishu:two",
                "other-thread",
                "/workspace/other",
            )
            registry.close()

            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}):
                listed = list_agent_sessions(
                    paths,
                    "codex",
                    project="le-wm-codex",
                    external_key="feishu:one",
                )
            self.assertIn("Codex 会话（2）", listed)
            self.assertIn("[当前] 019f8dfd", listed)
            self.assertIn("[可用] 019e81c0", listed)
            self.assertIn(
                "[当前] 019f8dfd\n目录：/workspace/old\n\n"
                "[可用] 019e81c0",
                listed,
            )
            self.assertNotIn("019f8dfd-130a-7a92-a115-f437e38e40a8", listed)
            self.assertNotIn("other-thread", listed)

            current = current_agent_sessions(
                paths,
                project="le-wm-codex",
                external_key="feishu:one",
            )
            self.assertIn("当前 Agent 会话", current)
            self.assertIn("类型：codex", current)
            self.assertIn("会话：019f8dfd", current)
            self.assertIn("目录：/workspace/old", current)

            switched = switch_agent_session(
                paths,
                "codex",
                "019e81c0",
                project="le-wm-codex",
                external_key="feishu:one",
            )
            self.assertEqual(
                "019e81c0-c415-7820-8921-2b32f1bee990",
                switched.thread_id,
            )

            current = current_agent_sessions(
                paths,
                project="le-wm-codex",
                external_key="feishu:one",
            )
            self.assertIn("会话：019e81c0", current)
            self.assertIn("目录：/workspace/le-wm", current)

    def test_agent_switch_rejects_unknown_provider_and_ambiguous_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            registry = SessionRegistry(paths.state_db)
            registry.bind(
                "cc_connect", "le-wm", "feishu:one", "abcd-1111", "/one"
            )
            registry.subscribe(
                "cc_connect", "le-wm", "feishu:one", "abcd-2222", "/two"
            )
            registry.close()

            with self.assertRaisesRegex(ValueError, "unsupported agent provider"):
                list_agent_sessions(paths, "opencode")
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                switch_agent_session(
                    paths,
                    "codex",
                    "abcd",
                    project="le-wm",
                    external_key="feishu:one",
                )

    def test_agent_list_prunes_routes_for_deleted_codex_rollouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = make_paths(root)
            paths.ensure_directories()
            codex_home = root / "codex"
            sessions_dir = codex_home / "sessions/2026/07/23"
            sessions_dir.mkdir(parents=True)
            kept_thread = "019e81c0-c415-7820-8921-2b32f1bee990"
            stale_thread = "019f8dfd-130a-7a92-a115-f437e38e40a8"
            (
                sessions_dir
                / f"rollout-2026-07-23T10-00-00-{kept_thread}.jsonl"
            ).write_text("{}\n")

            registry = SessionRegistry(paths.state_db)
            registry.bind(
                "cc_connect",
                "le-wm-codex",
                "feishu:one",
                stale_thread,
                "/workspace/stale",
            )
            registry.subscribe(
                "cc_connect",
                "le-wm-codex",
                "feishu:one",
                kept_thread,
                "/workspace/le-wm",
            )
            registry.close()
            connection = sqlite3.connect(paths.state_db)
            connection.execute(
                """
                UPDATE session_mappings
                SET updated_at = datetime('now', '-10 minutes')
                WHERE thread_id = ?
                """,
                (stale_thread,),
            )
            connection.execute(
                """
                UPDATE notification_subscriptions
                SET updated_at = datetime('now', '-10 minutes')
                WHERE thread_id = ?
                """,
                (stale_thread,),
            )
            connection.commit()
            connection.close()

            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}):
                listed = list_agent_sessions(
                    paths,
                    "codex",
                    project="le-wm-codex",
                    external_key="feishu:one",
                )

            self.assertIn("Codex 会话（1）", listed)
            self.assertIn("[可用] 019e81c0", listed)
            self.assertNotIn("019f8dfd", listed)
            registry = SessionRegistry(paths.state_db)
            self.assertIsNone(
                registry.get("cc_connect", "le-wm-codex", "feishu:one")
            )
            self.assertEqual(
                [kept_thread],
                [
                    route.thread_id
                    for route in registry.list_subscriptions(
                        "cc_connect", "le-wm-codex", "feishu:one"
                    )
                ],
            )
            registry.close()

    def test_agent_list_prunes_thread_without_rollout_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = make_paths(root)
            paths.ensure_directories()
            codex_home = root / "codex"
            (codex_home / "sessions").mkdir(parents=True)
            fresh_thread = "019f8e4b-130a-7a92-a115-f437e38e40a8"
            registry = SessionRegistry(paths.state_db)
            registry.bind(
                "cc_connect",
                "le-wm-codex",
                "feishu:one",
                fresh_thread,
                "/workspace/le-wm",
            )
            registry.close()

            with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}):
                listed = list_agent_sessions(
                    paths,
                    "codex",
                    project="le-wm-codex",
                    external_key="feishu:one",
                )

            self.assertIn("Codex 会话（0）", listed)
            self.assertNotIn("019f8e4b", listed)
            registry = SessionRegistry(paths.state_db)
            self.assertIsNone(
                registry.get(
                    "cc_connect", "le-wm-codex", "feishu:one"
                )
            )
            registry.close()


class ApprovalReplyTest(unittest.IsolatedAsyncioTestCase):
    async def test_reply_uses_stored_message_and_thread_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            registry = SessionRegistry(paths.state_db)
            registry.bind(
                "cc_connect", "le-wm", "feishu:chat:user", "thread-1", "/workspace"
            )
            registry.close()
            store = ApprovalStore(paths.state_db)
            store.register(
                "agent-notifier-approval:1234567890abcdef",
                "thread-1",
                "command",
                {"command": "date"},
            )
            store.set_feishu_message_id(
                "agent-notifier-approval:1234567890abcdef", "om_original"
            )
            store.close()

            with patch(
                "agent_notifier.cli.reply_approval_result_card",
                new=AsyncMock(return_value="om_reply"),
            ) as reply:
                await reply_approval_decision(
                    paths, "allow", "1234567890", "resolved"
                )

            reply.assert_awaited_once_with(
                project="le-wm",
                message_id="om_original",
                approval_id="1234567890",
                decision="allow",
                status="resolved",
                session_label="workspace",
                thread_id="thread-1",
                cwd="/workspace",
            )


if __name__ == "__main__":
    unittest.main()
