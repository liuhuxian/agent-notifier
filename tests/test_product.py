import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_notifier.cc_config import configure_project
from agent_notifier.config import Paths
from agent_notifier.hook_config import decode_command, gate_hooks
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
            'exec = "/home/me/.local/bin/agent-notifier decide --quiet allow {{1}}"',
            result,
        )
        self.assertIn(
            'exec = "/home/me/.local/bin/agent-notifier decide --quiet deny {{1}}"',
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
    def test_only_completion_and_permission_hooks_are_gated(self):
        source = '''{
          "hooks": {
            "Stop": [{"hooks": [{"command": "python3 notify.py"}]}],
            "PermissionRequest": [{"hooks": [{"command": "python3 approve.py"}]}],
            "UserPromptSubmit": [{"hooks": [{"command": "python3 context.py"}]}]
          }
        }'''
        updated, count = gate_hooks(source, "/opt/agent-notifier")
        self.assertEqual(2, count)
        self.assertIn("hook-gate --encoded", updated)
        self.assertIn("python3 context.py", updated)
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
