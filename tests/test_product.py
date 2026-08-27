import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_notifier.cc_config import configure_project
from agent_notifier.config import Paths, ProgressConfig
from agent_notifier.hook_config import (
    decode_command,
    gate_hooks,
    native_stop_hook,
)
from agent_notifier.native_hooks import build_completion_message, format_result
from agent_notifier.service import format_approval_message
from agent_notifier.versioning import parse_codex_version, parse_cc_connect_version


class PathsTest(unittest.TestCase):
    def test_xdg_paths_are_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "HOME": f"{tmp}/home",
                "XDG_CONFIG_HOME": f"{tmp}/config",
                "XDG_STATE_HOME": f"{tmp}/state",
                "XDG_RUNTIME_DIR": f"{tmp}/run",
            }
            with patch.dict(os.environ, env, clear=False):
                paths = Paths.from_environment()
            self.assertEqual(Path(env["XDG_CONFIG_HOME"]) / "agent-notifier", paths.config_dir)
            self.assertEqual(Path(env["XDG_RUNTIME_DIR"]) / "agent-notifier", paths.runtime_dir)


class VersionParsingTest(unittest.TestCase):
    def test_cli_version_outputs_are_parsed(self):
        self.assertEqual("0.145.0", parse_codex_version("codex-cli 0.145.0"))
        self.assertEqual("1.3.2", parse_cc_connect_version("cc-connect v1.3.2\ncommit: abc"))


class CCConfigTest(unittest.TestCase):
    def test_target_project_becomes_acp_without_changing_other_project(self):
        source = '''
[[projects]]
  name = "le-wm-codex"
  [projects.agent]
    type = "codex"
  [projects.agent.options]
    mode = "auto-edit"
    work_dir = "/workspace"

[[projects]]
name = "other"
[projects.agent]
type = "opencode"
'''
        result = configure_project(source, "le-wm-codex", "/home/me/.local/bin/agent-notifier")
        target, other = result.split('[[projects]]')[1:]
        self.assertIn('type = "acp"', target)
        self.assertIn('command = "/home/me/.local/bin/agent-notifier"', target)
        self.assertIn('args = ["acp"]', target)
        self.assertIn('type = "opencode"', other)
        self.assertEqual(1, target.count('type = "acp"'))
        self.assertNotIn('type = "codex"', target)
        self.assertIn(
            'exec = "/home/me/.local/bin/agent-notifier decide allow {{1}}"',
            result,
        )
        self.assertIn(
            'exec = "/home/me/.local/bin/agent-notifier decide deny {{1}}"',
            result,
        )
        self.assertIn('name = "opencode-approve"', result)
        self.assertIn('name = "opencode-deny"', result)
        self.assertIn(
            'opencode-reply-result --perm $1 once',
            result,
        )
        self.assertIn(
            'opencode-reply-result --perm {{1}} {{2}}',
            result,
        )
        self.assertNotIn('/permission/$1/reply', result)
        self.assertNotIn('name = "codex-switch"', result)
        self.assertIn(
            'exec = "/home/me/.local/bin/agent-notifier agent-list {{1}}"',
            result,
        )
        self.assertIn(
            'exec = "/home/me/.local/bin/agent-notifier agent-current"',
            result,
        )
        self.assertIn(
            'exec = "/home/me/.local/bin/agent-notifier agent-new {{1}}"',
            result,
        )
        self.assertIn(
            'exec = "/home/me/.local/bin/agent-notifier agent-switch {{1}} {{2}}"',
            result,
        )
        self.assertIn(
            'exec = "/home/me/.local/bin/agent-notifier agent-cmd {{1}}"',
            result,
        )
        self.assertIn(
            'exec = "/home/me/.local/bin/agent-notifier agent-help"',
            result,
        )

    def test_missing_project_is_rejected(self):
        with self.assertRaises(ValueError):
            configure_project('[[projects]]\nname = "other"\n', "missing", "/bin/tool")

    def test_existing_approval_commands_are_replaced_idempotently(self):
        source = '''
[[projects]]
name = "le-wm"
[projects.agent]
type = "codex"

[[commands]]
name = "codex-approve"
description = "old"
exec = "python3 old.py allow {{1}}"

[[commands]]
name = "codex-deny"
description = "old"
exec = "python3 old.py deny {{1}}"
'''
        once = configure_project(source, "le-wm", "/opt/agent-notifier")
        twice = configure_project(once, "le-wm", "/opt/agent-notifier")
        self.assertEqual(once, twice)
        self.assertNotIn("old.py", once)
        self.assertEqual(1, once.count('name = "codex-approve"'))
        self.assertEqual(1, once.count('name = "codex-deny"'))
        self.assertEqual(1, once.count('name = "opencode-select"'))
        self.assertEqual(1, once.count('name = "opencode-question-select"'))
        self.assertEqual(1, once.count('name = "opencode-question-submit"'))
        self.assertEqual(1, once.count('name = "agent-list"'))
        self.assertEqual(1, once.count('name = "agent-current"'))
        self.assertEqual(1, once.count('name = "agent-new"'))
        self.assertEqual(1, once.count('name = "agent-switch"'))
        self.assertEqual(1, once.count('name = "agent-cmd"'))
        self.assertEqual(1, once.count('name = "agent-help"'))

    def test_progress_settings_are_applied_to_target_platform_and_global_preview(self):
        source = '''
[[projects]]
name = "le-wm"
[projects.agent]
type = "codex"
[[projects.platforms]]
type = "feishu"
[projects.platforms.options]
app_id = "cli_x"
app_secret = "secret"
'''
        settings = ProgressConfig(
            onit=True,
            progress_card=True,
            stream_preview=True,
            stream_update_interval_ms=1800,
            notify_interruption=True,
        )

        result = configure_project(
            source, "le-wm", "/opt/agent-notifier", settings
        )

        self.assertIn('reaction_emoji = "OnIt"', result)
        self.assertIn('progress_style = "card"', result)
        self.assertIn("[stream_preview]", result)
        self.assertIn("enabled = true", result)
        self.assertIn("interval_ms = 1800", result)
        self.assertIn("min_delta_chars = 1", result)
        self.assertIn("[projects.display]", result)
        self.assertIn("thinking_messages = true", result)
        self.assertIn("tool_messages = true", result)

    def test_disabling_progress_hides_thinking_and_tool_messages(self):
        source = '''
[[projects]]
name = "le-wm"
[projects.agent]
type = "codex"
'''
        result = configure_project(
            source,
            "le-wm",
            "/opt/agent-notifier",
            ProgressConfig(progress_card=False),
        )

        self.assertIn("[projects.display]", result)
        self.assertIn("thinking_messages = false", result)
        self.assertIn("tool_messages = false", result)


class ApprovalMessageTest(unittest.TestCase):
    def test_message_contains_short_id_reason_command_and_actions(self):
        message = format_approval_message(
            "agent-notifier-approval:1234567890abcdef",
            {
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "reason": "需要测试权限",
                    "command": "touch /tmp/test",
                },
            },
        )
        self.assertIn("Codex 权限审批 [1234567890]", message)
        self.assertIn("原因：需要测试权限", message)
        self.assertIn("操作：touch /tmp/test", message)
        self.assertIn("/codex-approve 1234567890", message)
        self.assertIn("/codex-deny 1234567890", message)

class HookConfigTest(unittest.TestCase):
    def test_native_stop_hook_does_not_touch_permission_request(self):
        source = '{"hooks": {"PermissionRequest": [{"hooks": []}]}}'
        updated, changed = native_stop_hook(source, "/opt/agent-notifier")
        self.assertTrue(changed)
        self.assertIn("PermissionRequest", updated)
        self.assertIn("native-hook stop", decode_command(
            updated.split("hook-gate --encoded ", 1)[1].split('"', 1)[0]
        ))

    def test_native_stop_hook_is_idempotent(self):
        source, _ = native_stop_hook('{"hooks": {}}', "agent-notifier")
        updated, changed = native_stop_hook(source, "agent-notifier")
        self.assertFalse(changed)
        self.assertEqual(source, updated)

    def test_native_completion_keeps_result_layout(self):
        message, _ = build_completion_message({
            "hook_event_name": "Stop",
            "session_id": "019e81c0-c415",
            "cwd": "/workspace/le-wm",
            "last-assistant-message": "one\n\n- two\n```bash\nrm -f /tmp/x\n```",
        })
        self.assertIn(
            "**结果**：\n\none\n\n- two\n```bash\nrm -f /tmp/x\n```",
            message,
        )
        self.assertEqual("a\n\nb", format_result("a\n\nb"))
        self.assertEqual(
            "```text\na\n```", format_result("```text\na\n```")
        )

    def test_only_completion_hooks_are_gated(self):
        source = '''{
          "hooks": {
            "Stop": [{"hooks": [{"command": "python3 notify.py"}]}],
            "PermissionRequest": [{"hooks": [{"command": "python3 approve.py"}]}],
            "UserPromptSubmit": [{"hooks": [{"command": "python3 context.py"}]}]
          }
        }'''
        updated, count = gate_hooks(source, "/opt/agent-notifier")
        self.assertEqual(1, count)
        self.assertIn("hook-gate --encoded", updated)
        self.assertIn("python3 context.py", updated)
        self.assertIn("python3 approve.py", updated)
        encoded = updated.split("hook-gate --encoded ", 1)[1].split('"', 1)[0]
        self.assertEqual("python3 notify.py", decode_command(encoded))

    def test_gating_is_idempotent(self):
        source = '{"hooks":{"Stop":[{"hooks":[{"command":"notify"}]}]}}'
        once, count = gate_hooks(source, "agent-notifier")
        twice, second_count = gate_hooks(once, "agent-notifier")
        self.assertEqual(1, count)
        self.assertEqual(0, second_count)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()
