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
    @staticmethod
    def _write_notification_route(paths: Paths) -> None:
        paths.ensure_directories()
        paths.config_file.write_text(
            "[notification_routes.default]\n"
            'project = "le-wm-codex"\n'
            'receive_id_type = "chat_id"\n'
            'receive_id = "oc_notify"\n'
        )

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
                await service._notify_approval_request(
                    token, request, "cc_connect"
                )

            record = service.approval_store.find_by_prefix("1234567890")
            self.assertEqual("om_original", record.feishu_message_id)
            self.assertEqual(
                "workspace", send_card.await_args.kwargs["session_label"]
            )
            self.assertEqual("thread-1", send_card.await_args.kwargs["thread_id"])
            self.assertEqual("/workspace", send_card.await_args.kwargs["cwd"])
            service.registry.close()
            service.approval_store.close()

    async def test_terminal_approval_is_sent_to_notification_chat(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            self._write_notification_route(paths)
            service = SharedService(paths)
            service.registry = SessionRegistry(paths.state_db)
            service.approval_store = ApprovalStore(paths.state_db)
            service.registry.bind(
                "cc_connect",
                "le-wm-codex",
                "feishu:oc_notify:ou_user",
                "notify-thread",
                "/workspace",
            )
            service.registry.subscribe(
                "cc_connect",
                "le-wm-codex",
                "feishu:oc_interactive:ou_user",
                "thread-1",
                "/workspace",
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
                new=AsyncMock(return_value="om_notify"),
            ) as send_card:
                await service._notify_approval_request(
                    token, request, "terminal"
                )

            self.assertEqual("oc_notify", send_card.await_args.kwargs["receive_id"])
            self.assertEqual(
                "chat_id", send_card.await_args.kwargs["receive_id_type"]
            )
            self.assertEqual(
                "feishu:oc_interactive:ou_user",
                send_card.await_args.kwargs["session_key"],
            )
            service.registry.close()
            service.approval_store.close()

    async def test_completion_notification_includes_session_directory_and_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            self._write_notification_route(paths)
            service = SharedService(paths)
            service.registry = SessionRegistry(paths.state_db)
            service.registry.bind(
                "cc_connect",
                "le-wm-codex",
                "feishu:chat:user",
                "019e81c0-c415",
                "/users/huxian/project/le-wm",
            )
            with patch(
                "agent_notifier.service.send_text_message",
                new=AsyncMock(return_value="om_notify"),
            ) as send_message:
                await service._notify_terminal_completion(
                    "019e81c0-c415", "任务执行完成"
                )

            message = send_message.await_args.kwargs["text"]
            self.assertIn("Codex 回合已完成", message)
            self.assertIn("会话：le-wm | 019e81c0", message)
            self.assertIn("目录：/users/huxian/project/le-wm", message)
            self.assertIn("结果：\n\n任务执行完成", message)
            self.assertEqual(
                "oc_notify", send_message.await_args.kwargs["receive_id"]
            )
            self.assertEqual(
                "chat_id", send_message.await_args.kwargs["receive_id_type"]
            )
            service.registry.close()

    async def test_remote_progress_sends_complete_commentary_text(self):
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

            with patch(
                "agent_notifier.service.send_progress_message_with_onit",
                new=AsyncMock(
                    side_effect=[
                        ("om_progress_1", "reaction_1"),
                        ("om_progress_2", "reaction_2"),
                    ]
                ),
            ) as send_progress, patch(
                "agent_notifier.service.remove_message_reaction",
                new=AsyncMock(),
            ) as remove_reaction:
                await service._notify_remote_progress(
                    "019e81c0-c415", "阶段分析完成"
                )
                await service._notify_remote_progress(
                    "019e81c0-c415", "测试执行完成"
                )

            self.assertEqual(
                "阶段分析完成",
                send_progress.await_args_list[0].kwargs["text"],
            )
            self.assertEqual(
                "测试执行完成",
                send_progress.await_args_list[1].kwargs["text"],
            )
            remove_reaction.assert_awaited_once_with(
                project="le-wm-codex",
                message_id="om_progress_1",
                reaction_id="reaction_1",
            )
            service.registry.close()

    async def test_remote_completion_removes_latest_progress_onit(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            service = SharedService(paths)
            service._remote_progress_onit["thread-1"] = (
                "le-wm-codex",
                "om_progress",
                "reaction_latest",
            )

            with patch(
                "agent_notifier.service.remove_message_reaction",
                new=AsyncMock(),
            ) as remove_reaction:
                await service._finish_remote_progress("thread-1", "完成")

            remove_reaction.assert_awaited_once_with(
                project="le-wm-codex",
                message_id="om_progress",
                reaction_id="reaction_latest",
            )
            self.assertNotIn("thread-1", service._remote_progress_onit)

    async def test_token_usage_is_recorded_for_command_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            service = SharedService(paths)
            service.registry = SessionRegistry(paths.state_db)

            await service._record_token_usage(
                "thread-1",
                {
                    "total": {"totalTokens": 99},
                    "last": {"totalTokens": 9},
                },
            )

            self.assertEqual(
                99,
                service.registry.get_thread_token_usage("thread-1")[
                    "total"
                ]["totalTokens"],
            )
            service.registry.close()

    async def test_unexpected_app_server_exit_notifies_active_turns(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            service = SharedService(paths)
            service.registry = SessionRegistry(paths.state_db)
            service.registry.bind(
                "cc_connect",
                "le-wm-codex",
                "feishu:chat:user",
                "thread-1",
                "/workspace/le-wm",
            )
            service._send_to_mapping = AsyncMock()

            await service._notify_interrupted_turns(
                [("thread-1", "cc_connect")], returncode=17
            )

            message = service._send_to_mapping.await_args.args[1]
            self.assertIn("Codex 回合异常中断", message)
            self.assertIn("会话：le-wm | thread-1", message)
            self.assertIn("App Server 退出码：17", message)
            service.registry.close()


if __name__ == "__main__":
    unittest.main()
