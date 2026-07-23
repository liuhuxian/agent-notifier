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

    def test_new_active_session_keeps_both_notification_subscriptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            registry = SessionRegistry(db)
            registry.bind("cc_connect", "le-wm", "same", "thread-old", "/old")
            replaced = registry.bind(
                "cc_connect", "le-wm", "same", "thread-new", "/new"
            )
            self.assertEqual("thread-new", replaced.thread_id)
            self.assertEqual("/new", replaced.cwd)
            self.assertEqual("same", registry.find_by_thread("thread-old").external_key)
            self.assertEqual("same", registry.find_by_thread("thread-new").external_key)
            self.assertEqual(
                {"thread-old", "thread-new"},
                {
                    route.thread_id
                    for route in registry.list_subscriptions(
                        "cc_connect", "le-wm", "same"
                    )
                },
            )
            registry.close()

    def test_existing_active_routes_are_migrated_to_subscriptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            registry = SessionRegistry(db)
            registry.bind("cc_connect", "le-wm", "feishu:one", "thread-1", "/one")
            registry.close()

            connection = __import__("sqlite3").connect(db)
            connection.execute("DELETE FROM notification_subscriptions")
            connection.commit()
            connection.close()

            migrated = SessionRegistry(db)
            self.assertEqual(
                "feishu:one", migrated.find_by_thread("thread-1").external_key
            )
            migrated.close()

    def test_routes_can_be_filtered_by_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = SessionRegistry(Path(tmp) / "state.sqlite3")
            registry.bind("cc_connect", "one", "feishu:one", "thread-1", "/one")
            registry.bind("cc_connect", "two", "feishu:two", "thread-2", "/two")
            routes = registry.list_routes("cc_connect", "one")
            self.assertEqual(["feishu:one"], [route.external_key for route in routes])
            self.assertEqual(2, len(registry.list_routes("cc_connect")))
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
