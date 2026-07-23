import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_notifier.cc_config import configure_project
from agent_notifier.config import Paths
from agent_notifier.hook_config import decode_command, gate_hooks
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

    def test_missing_project_is_rejected(self):
        with self.assertRaises(ValueError):
            configure_project('[[projects]]\nname = "other"\n', "missing", "/bin/tool")


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
