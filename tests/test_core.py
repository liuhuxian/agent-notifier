import tempfile
import unittest
from pathlib import Path

from agent_notifier.approvals import ApprovalAlreadyResolved, ApprovalStore
from agent_notifier.policy import should_send_completion_notification
from agent_notifier.registry import SessionRegistry
from agent_notifier.versioning import CompatibilityError, require_supported_version


class VersioningTest(unittest.TestCase):
    def test_supported_versions_are_accepted(self):
        require_supported_version("codex", "0.145.0")
        require_supported_version("cc-connect", "1.3.2")

    def test_unknown_versions_are_rejected(self):
        with self.assertRaises(CompatibilityError):
            require_supported_version("codex", "0.146.0")


class PolicyTest(unittest.TestCase):
    def test_cc_connect_turn_does_not_duplicate_completion(self):
        self.assertFalse(should_send_completion_notification("cc_connect"))

    def test_terminal_turn_sends_completion(self):
        self.assertTrue(should_send_completion_notification("terminal"))


class RegistryTest(unittest.TestCase):
    def test_mapping_is_stable_across_registry_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            first = SessionRegistry(db)
            mapping = first.bind(
                adapter="cc_connect",
                project="le-wm",
                external_key="feishu:one",
                thread_id="thread-1",
                cwd="/workspace",
            )
            first.close()

            second = SessionRegistry(db)
            loaded = second.get("cc_connect", "le-wm", "feishu:one")
            self.assertEqual(mapping, loaded)
            self.assertEqual(mapping, second.find_by_thread("thread-1"))
            second.close()

    def test_new_session_replaces_mapping_for_same_external_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            registry = SessionRegistry(db)
            registry.bind("cc_connect", "le-wm", "same", "thread-old", "/old")
            replaced = registry.bind(
                "cc_connect", "le-wm", "same", "thread-new", "/new"
            )
            self.assertEqual("thread-new", replaced.thread_id)
            self.assertEqual("/new", replaced.cwd)
            self.assertIsNone(registry.find_by_thread("thread-old"))
            registry.close()


class ApprovalStoreTest(unittest.TestCase):
    def test_first_resolution_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ApprovalStore(Path(tmp) / "state.sqlite3")
            store.register("approval-1", "thread-1", "command", {"command": "date"})
            resolved = store.resolve("approval-1", "allow", "terminal")
            self.assertEqual("terminal", resolved.resolved_by)
            with self.assertRaises(ApprovalAlreadyResolved):
                store.resolve("approval-1", "deny", "cc_connect")
            store.close()


if __name__ == "__main__":
    unittest.main()
