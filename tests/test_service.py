import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_notifier.approvals import ApprovalStore
from agent_notifier.config import Paths
from agent_notifier.registry import SessionRegistry
from agent_notifier.service import SharedService


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


class ApprovalNotificationTest(unittest.IsolatedAsyncioTestCase):
    async def test_sent_approval_card_message_id_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            service = SharedService(paths)
            service.registry = SessionRegistry(paths.state_db)
            service.approval_store = ApprovalStore(paths.state_db)
            service.registry.bind(
                "cc_connect", "le-wm", "feishu:chat:user", "thread-1", "/workspace"
            )
            token = "agent-notifier-approval:1234567890abcdef"
            request = {
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread-1",
                    "reason": "test",
                    "command": "date",
                },
            }
            service.approval_store.register(
                token, "thread-1", request["method"], request
            )

            with patch(
                "agent_notifier.service.send_approval_card",
                new=AsyncMock(return_value="om_original"),
            ) as send_card:
                await service._notify_approval_request(token, request)

            record = service.approval_store.find_by_prefix("1234567890")
            self.assertEqual("om_original", record.feishu_message_id)
            self.assertEqual(
                "workspace", send_card.await_args.kwargs["session_label"]
            )
            self.assertEqual("thread-1", send_card.await_args.kwargs["thread_id"])
            self.assertEqual("/workspace", send_card.await_args.kwargs["cwd"])
            service.registry.close()
            service.approval_store.close()

    async def test_completion_notification_includes_session_directory_and_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            service = SharedService(paths)
            service.registry = SessionRegistry(paths.state_db)
            service.registry.bind(
                "cc_connect",
                "le-wm-codex",
                "feishu:chat:user",
                "019e81c0-c415",
                "/users/huxian/project/le-wm",
            )
            service._send_to_mapping = AsyncMock()

            await service._notify_terminal_completion(
                "019e81c0-c415", "任务执行完成"
            )

            message = service._send_to_mapping.await_args.args[1]
            self.assertIn("Codex 回合已完成", message)
            self.assertIn("会话：le-wm | 019e81c0", message)
            self.assertIn("目录：/users/huxian/project/le-wm", message)
            self.assertIn("结果：\n任务执行完成", message)
            service.registry.close()

    async def test_remote_completion_sends_only_final_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            service = SharedService(paths)
            service.registry = SessionRegistry(paths.state_db)
            service.registry.bind(
                "cc_connect",
                "le-wm-codex",
                "feishu:chat:user",
                "019e81c0-c415",
                "/users/huxian/project/le-wm",
            )
            service._send_to_mapping = AsyncMock()

            await service._notify_remote_completion(
                "019e81c0-c415", "飞书最终回复"
            )

            self.assertEqual(
                "飞书最终回复",
                service._send_to_mapping.await_args.args[1],
            )
            service.registry.close()


if __name__ == "__main__":
    unittest.main()
