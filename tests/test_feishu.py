import tempfile
import unittest
from pathlib import Path

from agent_notifier.feishu import (
    build_approval_card,
    build_approval_result_card,
    load_feishu_settings,
)


class FeishuCardTest(unittest.TestCase):
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
        content = card["elements"][0]["content"]
        self.assertIn("会话：le-wm | 019e81c0", content)
        self.assertIn("目录：`/users/huxian/project/le-wm`", content)
        actions = card["elements"][-1]["actions"]
        allow, deny = actions
        self.assertEqual(
            "cmd:/codex-approve 1234567890", allow["value"]["action"]
        )
        self.assertEqual(
            "cmd:/codex-deny 1234567890", deny["value"]["action"]
        )
        self.assertNotIn("after_click", allow["value"])
        self.assertNotIn("after_click", deny["value"])
        self.assertEqual("feishu:chat:user", allow["value"]["session_key"])

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

        self.assertEqual("green", allowed["header"]["template"])
        self.assertEqual("red", denied["header"]["template"])
        self.assertEqual("orange", handled["header"]["template"])
        self.assertIn("已允许", allowed["header"]["title"]["content"])
        self.assertIn("已拒绝", denied["header"]["title"]["content"])
        self.assertIn("已经处理", handled["header"]["title"]["content"])
        self.assertIn(
            "会话：le-wm | 019e81c0", allowed["elements"][0]["content"]
        )
        self.assertIn(
            "目录：`/users/huxian/project/le-wm`",
            allowed["elements"][0]["content"],
        )


if __name__ == "__main__":
    unittest.main()
