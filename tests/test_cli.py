import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_notifier.approvals import ApprovalStore
from agent_notifier.cli import (
    _resume_thread_id,
    activate_terminal_thread,
    approval_decision_message,
    bind_terminal_thread,
    build_parser,
    reply_approval_decision,
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
