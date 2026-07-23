import asyncio
import tempfile
import unittest
from pathlib import Path

from agent_notifier.codex.backend import CodexBackend
from agent_notifier.registry import SessionRegistry


class FakeRPC:
    def __init__(self):
        self.calls = []
        self.events = asyncio.Queue()

    async def call(self, method, params):
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-new"}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            return {"turn": {"id": "turn-new"}}
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}
        if method == "turn/interrupt":
            return {}
        raise AssertionError(method)

    async def next_event(self):
        return await self.events.get()


class CodexBackendTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = SessionRegistry(Path(self.tmp.name) / "state.sqlite3")
        self.rpc = FakeRPC()
        self.backend = CodexBackend(self.rpc, self.registry)

    async def asyncTearDown(self):
        await self.backend.close()
        self.registry.close()
        self.tmp.cleanup()

    async def test_start_thread_binds_external_session(self):
        thread_id = await self.backend.start_thread(
            "/workspace", "le-wm", "feishu:one"
        )
        self.assertEqual("thread-new", thread_id)
        mapping = self.registry.get("cc_connect", "le-wm", "feishu:one")
        self.assertEqual("thread-new", mapping.thread_id)

    async def test_resume_thread_binds_external_session(self):
        thread_id = await self.backend.resume_thread(
            "thread-old", "/workspace", "le-wm", "feishu:one"
        )
        self.assertEqual("thread-old", thread_id)
        mapping = self.registry.get("cc_connect", "le-wm", "feishu:one")
        self.assertEqual("thread-old", mapping.thread_id)
        self.assertEqual(
            "feishu:one",
            self.registry.find_by_thread("thread-old").external_key,
        )

    async def test_switching_active_session_keeps_previous_thread_notifications(self):
        await self.backend.resume_thread(
            "thread-old", "/workspace", "le-wm", "feishu:one"
        )
        await self.backend.resume_thread(
            "thread-new", "/workspace", "le-wm", "feishu:one"
        )

        active = self.registry.get("cc_connect", "le-wm", "feishu:one")
        self.assertEqual("thread-new", active.thread_id)
        self.assertEqual(
            "feishu:one",
            self.registry.find_by_thread("thread-old").external_key,
        )

    async def test_resolve_active_thread_prefers_registry_target(self):
        self.registry.bind(
            "cc_connect", "le-wm", "feishu:one", "thread-active", "/workspace"
        )
        self.assertEqual(
            "thread-active",
            self.backend.resolve_active_thread(
                "le-wm", "feishu:one", "thread-fallback"
            ),
        )

    async def test_prompt_streams_until_matching_turn_completes(self):
        output = []

        async def emit(event):
            output.append(event)

        task = asyncio.create_task(
            self.backend.prompt("thread-1", "hello", "cc_connect", emit)
        )
        await asyncio.sleep(0)
        await self.rpc.events.put(
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread-1", "turnId": "turn-new", "delta": "hi"},
            }
        )
        await self.rpc.events.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-new", "status": "completed"},
                },
            }
        )
        result = await asyncio.wait_for(task, 1)
        self.assertEqual([{"kind": "text", "text": "hi"}], output)
        self.assertEqual("end_turn", result["stopReason"])
        turn_start = next(params for method, params in self.rpc.calls if method == "turn/start")
        self.assertNotIn("responsesapiClientMetadata", turn_start)

    async def test_active_turn_uses_steer(self):
        self.backend.active_turns["thread-1"] = "turn-active"

        async def emit(_event):
            pass

        task = asyncio.create_task(
            self.backend.prompt("thread-1", "correction", "cc_connect", emit)
        )
        await asyncio.sleep(0)
        self.assertEqual("turn/steer", self.rpc.calls[-1][0])
        self.assertNotIn("responsesapiClientMetadata", self.rpc.calls[-1][1])
        await self.rpc.events.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-active", "status": "completed"},
                },
            }
        )
        await asyncio.wait_for(task, 1)

    async def test_failed_turn_surfaces_codex_error(self):
        async def emit(_event):
            pass

        task = asyncio.create_task(
            self.backend.prompt("thread-1", "hello", "cc_connect", emit)
        )
        await asyncio.sleep(0)
        await self.rpc.events.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {
                        "id": "turn-new",
                        "status": "failed",
                        "error": {"message": "workspace is out of credits"},
                    },
                },
            }
        )
        with self.assertRaisesRegex(RuntimeError, "workspace is out of credits"):
            await asyncio.wait_for(task, 1)


if __name__ == "__main__":
    unittest.main()
