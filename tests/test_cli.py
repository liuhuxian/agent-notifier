import tempfile
import unittest
from pathlib import Path

from agent_notifier.cli import (
    _resume_thread_id,
    bind_terminal_thread,
    build_parser,
)
from agent_notifier.config import Paths
from agent_notifier.registry import SessionRegistry


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


class CodexBindingTest(unittest.TestCase):
    def test_resume_thread_is_extracted_from_passthrough_args(self):
        self.assertEqual(
            "thread-1", _resume_thread_id(["--no-alt-screen", "resume", "thread-1"])
        )
        self.assertIsNone(_resume_thread_id(["--no-alt-screen"]))

    def test_codex_parser_keeps_notifier_option_out_of_passthrough_args(self):
        args = build_parser().parse_args(
            ["codex", "--notify-project", "le-wm", "resume", "thread-1"]
        )
        self.assertEqual("le-wm", args.notify_project)
        self.assertEqual(["resume", "thread-1"], args.codex_args)

    def test_unique_route_is_rebound_to_resumed_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            registry = SessionRegistry(paths.state_db)
            registry.bind(
                "cc_connect", "le-wm", "feishu:one", "thread-old", "/workspace"
            )
            registry.close()

            mapping = bind_terminal_thread(
                paths, "thread-new", project="le-wm", cwd="/new-workspace"
            )
            self.assertEqual("thread-new", mapping.thread_id)
            self.assertEqual("/new-workspace", mapping.cwd)

            registry = SessionRegistry(paths.state_db)
            self.assertIsNone(registry.find_by_thread("thread-old"))
            self.assertEqual(
                "feishu:one",
                registry.find_by_thread("thread-new").external_key,
            )
            registry.close()

    def test_ambiguous_routes_require_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = make_paths(Path(tmp))
            paths.ensure_directories()
            registry = SessionRegistry(paths.state_db)
            registry.bind("cc_connect", "one", "feishu:one", "thread-1", "/one")
            registry.bind("cc_connect", "two", "feishu:two", "thread-2", "/two")
            registry.close()

            with self.assertRaisesRegex(ValueError, "specify --notify-project"):
                bind_terminal_thread(paths, "thread-new")


if __name__ == "__main__":
    unittest.main()
