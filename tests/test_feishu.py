import tempfile
import unittest
from pathlib import Path

from agent_notifier.feishu import build_approval_card, load_feishu_settings


class FeishuCardTest(unittest.TestCase):
    def test_buttons_execute_commands_and_replace_card_after_click(self):
        card = build_approval_card(
            approval_id="1234567890",
            reason="需要权限",
            operation="touch /tmp/test",
            session_key="feishu:chat:user",
        )
        actions = card["elements"][-1]["actions"]
        allow, deny = actions
        self.assertEqual(
            "cmd:/codex-approve 1234567890", allow["value"]["action"]
        )
        self.assertEqual(
            "cmd:/codex-deny 1234567890", deny["value"]["action"]
        )
        self.assertEqual("green", allow["value"]["after_click"]["color"])
        self.assertIn(
            "你的选择：**允许**",
            allow["value"]["after_click"]["markdown"],
        )
        self.assertEqual("red", deny["value"]["after_click"]["color"])
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


if __name__ == "__main__":
    unittest.main()
