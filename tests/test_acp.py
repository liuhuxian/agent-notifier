import asyncio
import io
import json
import unittest

from agent_notifier.acp.handler import ACPHandler
from agent_notifier.acp.server import ACPStdioServer


class FakeBackend:
    def __init__(self):
        self.loaded = []
        self.started = []
        self.prompts = []

    async def start_thread(self, cwd, project, external_key):
        self.started.append((cwd, project, external_key))
        return "thread-new"

    async def resume_thread(self, thread_id, cwd, project, external_key):
        self.loaded.append((thread_id, cwd, project, external_key))
        return thread_id

    async def prompt(self, thread_id, text, origin, emit):
        self.prompts.append((thread_id, text, origin))
        await emit({"kind": "text", "text": "hello "})
        await emit({"kind": "text", "text": "world"})
        return {"stopReason": "end_turn"}

    async def cancel(self, thread_id):
        return None


class ACPHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = FakeBackend()
        self.events = []

        async def emit(method, params):
            self.events.append((method, params))

        self.handler = ACPHandler(
            self.backend,
            cwd="/workspace",
            project="le-wm",
            external_key="feishu:one",
            emit=emit,
        )

    async def test_initialize_advertises_load_session(self):
        result = await self.handler.request("initialize", {"protocolVersion": 1})
        self.assertEqual(1, result["protocolVersion"])
        self.assertTrue(result["agentCapabilities"]["loadSession"])

    async def test_new_and_load_use_codex_thread_ids(self):
        created = await self.handler.request("session/new", {"cwd": "/workspace"})
        self.assertEqual("thread-new", created["sessionId"])
        loaded = await self.handler.request(
            "session/load", {"sessionId": "thread-old", "cwd": "/other"}
        )
        self.assertEqual("thread-old", loaded["sessionId"])
        self.assertEqual(
            [("thread-old", "/other", "le-wm", "feishu:one")],
            self.backend.loaded,
        )

    async def test_prompt_streams_acp_agent_message_chunks(self):
        await self.handler.request("session/load", {"sessionId": "thread-old"})
        result = await self.handler.request(
            "session/prompt",
            {"sessionId": "thread-old", "prompt": [{"type": "text", "text": "go"}]},
        )
        self.assertEqual("end_turn", result["stopReason"])
        chunks = [p["update"]["content"]["text"] for m, p in self.events]
        self.assertEqual(["hello ", "world"], chunks)
        self.assertEqual([("thread-old", "go", "cc_connect")], self.backend.prompts)

    async def test_codex_approval_round_trips_through_cc_connect(self):
        output = io.StringIO()
        server = ACPStdioServer(self.backend, io.StringIO(), output)
        task = asyncio.create_task(
            server.handle_codex_request(
                "item/commandExecution/requestApproval",
                {"threadId": "thread-old", "itemId": "item-1", "command": "date"},
            )
        )
        await asyncio.sleep(0)
        request = json.loads(output.getvalue().splitlines()[-1])
        await server.process(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "outcome": {"outcome": "selected", "optionId": "allow_once"}
                },
            }
        )
        self.assertEqual({"decision": "accept"}, await asyncio.wait_for(task, 1))

    async def test_legacy_approval_uses_legacy_decision_shape(self):
        output = io.StringIO()
        server = ACPStdioServer(self.backend, io.StringIO(), output)
        task = asyncio.create_task(
            server.handle_codex_request(
                "execCommandApproval",
                {"threadId": "thread-old", "itemId": "item-1", "command": "date"},
            )
        )
        await asyncio.sleep(0)
        request = json.loads(output.getvalue().splitlines()[-1])
        await server.process(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "outcome": {"outcome": "selected", "optionId": "allow_once"}
                },
            }
        )
        self.assertEqual(
            {"decision": "approved"}, await asyncio.wait_for(task, 1)
        )


if __name__ == "__main__":
    unittest.main()
