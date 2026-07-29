import tempfile
import unittest
from pathlib import Path

from agent_notifier.config import NotifierConfig
from agent_notifier.notification_routing import (
    agent_route_name,
    resolve_thread_route_name,
)
from agent_notifier.registry import SessionRegistry


class NotificationRoutingTest(unittest.TestCase):
    def _config(self, root: Path) -> NotifierConfig:
        path = root / "config.toml"
        path.write_text(
            "[agent_routes]\n"
            'codex = "codex_p2p"\n'
            'opencode = "opencode_group"\n\n'
            "[notification_routes.default]\n"
            'project = "notify"\nreceive_id = "group_b"\n\n'
            "[notification_routes.codex_p2p]\n"
            'project = "codex"\nreceive_id_type = "open_id"\n'
            'receive_id = "ou_codex"\n\n'
            "[notification_routes.opencode_group]\n"
            'project = "opencode"\nreceive_id = "oc_group_a"\n',
            encoding="utf-8",
        )
        return NotifierConfig.load(path)

    def test_agent_route_defaults_are_configurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            self.assertEqual("codex_p2p", agent_route_name(config, "codex"))
            self.assertEqual(
                "opencode_group", agent_route_name(config, "opencode")
            )
            self.assertEqual("default", agent_route_name(config, "codex", "default"))

    def test_persisted_thread_route_wins_over_mapping_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root)
            registry = SessionRegistry(root / "state.sqlite3")
            mapping = registry.bind(
                "cc_connect",
                "codex",
                "feishu:ou_codex:terminal:thread-1",
                "thread-1",
                "/workspace",
            )
            registry.set_thread_route(
                "thread-1", "codex", "codex_p2p", mapping.project,
                mapping.external_key, mapping.cwd,
            )
            self.assertEqual(
                "codex_p2p",
                resolve_thread_route_name(config, registry, "thread-1", mapping),
            )
            registry.close()

    def test_mapping_receive_id_infers_opencode_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root)
            registry = SessionRegistry(root / "state.sqlite3")
            mapping = registry.bind(
                "cc_connect",
                "opencode",
                "feishu:oc_group_a:terminal:session-1",
                "session-1",
                "/workspace",
            )
            self.assertEqual(
                "opencode_group",
                resolve_thread_route_name(config, registry, "session-1", mapping),
            )
            registry.close()

    def test_unbound_thread_uses_explicit_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root)
            registry = SessionRegistry(root / "state.sqlite3")
            self.assertEqual(
                "default",
                resolve_thread_route_name(
                    config, registry, "missing", fallback="default"
                ),
            )
            registry.close()


if __name__ == "__main__":
    unittest.main()
