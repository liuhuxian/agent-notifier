import tempfile
import unittest
from pathlib import Path

from agent_notifier.config import (
    NotifierConfig,
    Paths,
    initialize_user_config,
)
from agent_notifier.setup import configure_routes


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


class NotifierConfigTest(unittest.TestCase):
    def test_missing_config_uses_documented_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = NotifierConfig.load(Path(tmp) / "missing.toml")

        self.assertTrue(config.progress.onit)
        self.assertTrue(config.progress.progress_card)
        self.assertFalse(config.progress.stream_preview)
        self.assertEqual(2000, config.progress.stream_update_interval_ms)
        self.assertTrue(config.progress.notify_interruption)
        self.assertTrue(config.progress.moving_onit)

    def test_partial_config_overrides_only_selected_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[progress]\n"
                "progress_card = false\n"
                "stream_update_interval_ms = 3500\n"
                "moving_onit = false\n"
            )
            config = NotifierConfig.load(path)

        self.assertFalse(config.progress.progress_card)
        self.assertFalse(config.progress.stream_preview)
        self.assertEqual(3500, config.progress.stream_update_interval_ms)
        self.assertFalse(config.progress.moving_onit)

    def test_notification_route_is_loaded_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[notification_routes.default]\n"
                'project = "le-wm-codex"\n'
                'receive_id_type = "chat_id"\n'
                'receive_id = "oc_notify"\n'
            )
            config = NotifierConfig.load(path)

        route = config.notification_routes["default"]
        self.assertEqual("le-wm-codex", route.project)
        self.assertEqual("chat_id", route.receive_id_type)
        self.assertEqual("oc_notify", route.receive_id)

    def test_notification_route_rejects_unsupported_receive_id_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[notification_routes.default]\n"
                'project = "le-wm-codex"\n'
                'receive_id_type = "email"\n'
                'receive_id = "nobody@example.com"\n'
            )
            with self.assertRaisesRegex(ValueError, "receive_id_type"):
                NotifierConfig.load(path)

    def test_initialize_config_is_idempotent_and_preserves_user_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            first = initialize_user_config(paths)
            self.assertTrue(first.created)
            self.assertIn("[progress]", first.path.read_text())

            first.path.write_text("[progress]\nonit = false\n")
            second = initialize_user_config(paths)

            self.assertFalse(second.created)
            self.assertEqual("[progress]\nonit = false\n", second.path.read_text())

    def test_setup_writes_secret_free_routes_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            initialize_user_config(paths)
            configure_routes(paths, "le-wm-codex", "oc_group_a", "oc_group_b")
            configure_routes(paths, "le-wm-codex", "oc_group_a2", "oc_group_b2")
            text = paths.config_file.read_text()

        self.assertIn('oc_group_a2 = "opencode"', text)
        self.assertIn('receive_id = "oc_group_b2"', text)
        self.assertNotIn('oc_group_a = "opencode"', text)
        self.assertNotIn("secret", text.lower())


if __name__ == "__main__":
    unittest.main()
