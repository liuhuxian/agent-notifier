import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_notifier.feishu import (
    build_approval_card,
    build_approval_result_card,
    build_markdown_card_v2,
    build_opencode_approval_card,
    build_opencode_question_card,
    load_feishu_settings,
    send_progress_message_with_onit,
    send_markdown_message,
    send_text_message,
)


class FeishuCardTest(unittest.TestCase):
    def test_opencode_question_card_renders_ordered_choices(self):
        card = build_opencode_question_card(
            "que-123", "ses-456789", [{
                "header": "选择模式",
                "question": "选择一个模式",
                "options": [
                    {"label": "自动", "description": "a"},
                    {"label": "手动", "description": "b"},
                ],
                "custom": False,
            }],
        )
        self.assertEqual("OpenCode 问题选择", card["header"]["title"]["content"])
        self.assertIn("选择一个模式", card["body"]["elements"][1]["content"])
        buttons = card["body"]["elements"][2]["columns"]
        self.assertEqual(
            "cmd:/opencode-question-select que-123 0 1",
            buttons[1]["elements"][0]["behaviors"][0]["value"]["action"],
        )
        self.assertEqual(
            "cmd:/opencode-question-submit que-123",
            card["body"]["elements"][-1]["behaviors"][0]["value"]["action"],
        )

    def test_opencode_card_renders_dynamic_choices(self):
        options = [
            {"option_id": "once", "label": "允许一次"},
            {"option_id": "always", "label": "始终允许"},
            {"option_id": "reject", "label": "拒绝"},
        ]
        card = build_opencode_approval_card(
            "perm-123", "write", "/tmp/a", options=options
        )
        self.assertEqual("2.0", card["schema"])
        buttons = card["body"]["elements"][-1]["columns"]
        self.assertEqual(3, len(buttons))
        actions = [
            col["elements"][0]["behaviors"][0]["value"]["action"]
            for col in buttons
        ]
        self.assertEqual("cmd:/opencode-approve perm-123", actions[0])
        self.assertEqual("cmd:/opencode-select perm-123 always", actions[1])
        self.assertEqual("cmd:/opencode-deny perm-123", actions[2])

    def test_buttons_execute_real_approval_commands_without_replacing_card(self):
        card = build_approval_card(
            approval_id="1234567890",
            reason="需要权限",
            operation="touch /tmp/test",
            session_key="feishu:chat:user",
            session_label="le-wm",
            thread_id="019e81c0-c415",
            cwd="/users/huxian/project/le-wm",
        )
        self.assertEqual("2.0", card["schema"])
        self.assertEqual("orange", card["header"]["template"])
        content = card["body"]["elements"][0]["content"]
        self.assertIn("**会话**：le-wm | 019e81c0", content)
        self.assertIn("**目录**：`/users/huxian/project/le-wm`", content)
        button_set = card["body"]["elements"][-1]
        self.assertEqual("column_set", button_set["tag"])
        self.assertEqual(2, len(button_set["columns"]))
        allow = button_set["columns"][0]["elements"][0]
        deny = button_set["columns"][1]["elements"][0]
        self.assertEqual(
            "cmd:/codex-approve 1234567890",
            allow["behaviors"][0]["value"]["action"],
        )
        self.assertEqual(
            "cmd:/codex-deny 1234567890",
            deny["behaviors"][0]["value"]["action"],
        )
        self.assertNotIn("after_click", allow["behaviors"][0]["value"])
        self.assertNotIn("after_click", deny["behaviors"][0]["value"])
        self.assertEqual(
            "feishu:chat:user",
            allow["behaviors"][0]["value"]["session_key"],
        )
        self.assertEqual("button", allow["tag"])

    def test_project_feishu_credentials_are_loaded(self):
        # TOML is intentionally written directly to verify the deployed parser.
        source = """
[[projects]]
name = "le-wm"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "app"
app_secret = "secret"
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(source)
            settings = load_feishu_settings("le-wm", path)
        self.assertEqual("app", settings["app_id"])
        self.assertEqual("secret", settings["app_secret"])
        self.assertEqual("https://open.feishu.cn", settings["domain"])

    def test_approval_result_cards_use_status_header_colors(self):
        context = {
            "session_label": "le-wm",
            "thread_id": "019e81c0-c415",
            "cwd": "/users/huxian/project/le-wm",
        }
        allowed = build_approval_result_card(
            "1234567890", "allow", "resolved", **context
        )
        denied = build_approval_result_card(
            "1234567890", "deny", "resolved", **context
        )
        handled = build_approval_result_card(
            "1234567890", "allow", "already_resolved", **context
        )
        preserved = build_approval_result_card(
            "1234567890", "allow", "resolved", reason="需要权限", operation="touch /tmp/test", **context
        )

        self.assertEqual("green", allowed["header"]["template"])
        self.assertEqual("red", denied["header"]["template"])
        self.assertEqual("grey", handled["header"]["template"])
        self.assertEqual(
            "已允许：Codex 权限请求", allowed["header"]["title"]["content"]
        )
        self.assertEqual(
            "已拒绝：Codex 权限请求", denied["header"]["title"]["content"]
        )
        self.assertEqual(
            "已处理：Codex 权限请求", handled["header"]["title"]["content"]
        )
        self.assertIn("**原因**：需要权限", preserved["body"]["elements"][0]["content"])
        self.assertIn("**操作**：`touch /tmp/test`", preserved["body"]["elements"][0]["content"])
        self.assertIn(
            "**会话**：le-wm | 019e81c0",
            allowed["body"]["elements"][0]["content"],
        )
        self.assertIn(
            "**目录**：`/users/huxian/project/le-wm`",
            allowed["body"]["elements"][0]["content"],
        )

    def test_expired_result_card_is_grey_and_explains_restart(self):
        expired = build_approval_result_card(
            "1234567890",
            "deny",
            "expired",
            "le-wm",
            "thread-1",
            "/workspace",
            "permission request",
            "date",
        )
        self.assertEqual("grey", expired["header"]["template"])
        self.assertEqual(
            "已失效：Codex 权限请求",
            expired["header"]["title"]["content"],
        )
        self.assertIn("重启已失效", expired["body"]["elements"][0]["content"])


class FeishuMessageTest(unittest.IsolatedAsyncioTestCase):
    def test_completion_card_v2_has_fixed_blue_title_and_body(self):
        card = build_markdown_card_v2(
            "Codex 回合已完成",
            "会话：le-wm | 019e81c0\n目录：/users/huxian/project/le-wm\n结果：\n\n```text\npass\n```",
        )
        self.assertEqual("2.0", card["schema"])
        self.assertEqual("blue", card["header"]["template"])
        self.assertEqual("Codex 回合已完成", card["header"]["title"]["content"])
        self.assertEqual(
            "会话：le-wm | 019e81c0\n目录：/users/huxian/project/le-wm\n结果：\n\n```text\npass\n```",
            card["body"]["elements"][0]["content"],
        )

    async def test_text_message_uses_configured_receive_id_type(self):
        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        with patch(
            "agent_notifier.feishu.load_feishu_settings",
            return_value={
                "app_id": "app",
                "app_secret": "secret",
                "domain": "https://open.feishu.cn",
            },
        ), patch(
            "agent_notifier.feishu.ClientSession",
            return_value=FakeSession(),
        ), patch(
            "agent_notifier.feishu._tenant_access_token",
            new=AsyncMock(return_value="token"),
        ), patch(
            "agent_notifier.feishu._post_json",
            new=AsyncMock(
                return_value={"data": {"message_id": "om_notify"}}
            ),
        ) as post_json:
            message_id = await send_text_message(
                project="le-wm-codex",
                receive_id="oc_notify",
                receive_id_type="chat_id",
                text="pipeline done",
            )

        self.assertEqual("om_notify", message_id)
        self.assertIn(
            "receive_id_type=chat_id",
            post_json.await_args.args[1],
        )
        self.assertEqual(
            "oc_notify", post_json.await_args.args[2]["receive_id"]
        )

    async def test_markdown_message_uses_interactive_markdown_card(self):
        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        with patch(
            "agent_notifier.feishu.load_feishu_settings",
            return_value={
                "app_id": "app",
                "app_secret": "secret",
                "domain": "https://open.feishu.cn",
            },
        ), patch(
            "agent_notifier.feishu.ClientSession",
            return_value=FakeSession(),
        ), patch(
            "agent_notifier.feishu._tenant_access_token",
            new=AsyncMock(return_value="token"),
        ), patch(
            "agent_notifier.feishu._post_json",
            new=AsyncMock(return_value={"data": {"message_id": "om_card"}}),
        ) as post_json:
            message_id = await send_markdown_message(
                project="le-wm-codex",
                receive_id="oc_notify",
                receive_id_type="chat_id",
                text="```text\nhello\n```",
            )

        self.assertEqual("om_card", message_id)
        payload = post_json.await_args.args[2]
        self.assertEqual("interactive", payload["msg_type"])
        card = json.loads(payload["content"])
        self.assertEqual("2.0", card["schema"])
        self.assertEqual("通知", card["header"]["title"]["content"])
        self.assertEqual("markdown", card["body"]["elements"][0]["tag"])
        self.assertEqual("```text\nhello\n```", card["body"]["elements"][0]["content"])

    async def test_progress_message_uses_v2_card_before_adding_onit(self):
        class FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        with patch(
            "agent_notifier.feishu.load_feishu_settings",
            return_value={
                "app_id": "app",
                "app_secret": "secret",
                "domain": "https://open.feishu.cn",
            },
        ), patch(
            "agent_notifier.feishu.ClientSession",
            return_value=FakeSession(),
        ), patch(
            "agent_notifier.feishu._tenant_access_token",
            new=AsyncMock(return_value="token"),
        ), patch(
            "agent_notifier.feishu._post_json",
            new=AsyncMock(
                side_effect=[
                    {"data": {"message_id": "om_progress"}},
                    {"data": {"reaction_id": "reaction"}},
                ]
            ),
        ) as post_json:
            message_id, reaction_id = await send_progress_message_with_onit(
                project="le-wm-codex",
                receive_id="ou_notify",
                text="**处理中**\n\n```text\nstep 1\n```",
            )

        self.assertEqual(("om_progress", "reaction"), (message_id, reaction_id))
        payload = post_json.await_args_list[0].args[2]
        self.assertEqual("interactive", payload["msg_type"])
        card = json.loads(payload["content"])
        self.assertEqual("2.0", card["schema"])
        self.assertEqual("**处理中**\n\n```text\nstep 1\n```", card["body"]["elements"][0]["content"])


if __name__ == "__main__":
    unittest.main()
