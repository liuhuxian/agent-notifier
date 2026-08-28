import asyncio
import io
import json
import unittest
from uuid import UUID

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
        await emit({"kind": "status", "text": "正在思考"})
        await emit({
            "kind": "tool_start",
            "tool_call_id": "tool-1",
            "title": "正在运行测试",
            "tool_kind": "execute",
            "raw_input": {"command": "pytest -q"},
        })
        await emit({
            "kind": "tool_complete",
            "tool_call_id": "tool-1",
            "status": "completed",
        })
        await emit({"kind": "text", "text": "hello "})
        await emit({"kind": "text", "text": "world"})
        return {"stopReason": "end_turn"}

    def resolve_active_thread(self, project, external_key, fallback):
        return getattr(self, "active_thread", None) or fallback

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

    async def test_acp_uses_uuid_and_keeps_backend_thread_id_private(self):
        created = await self.handler.request("session/new", {"cwd": "/workspace"})
        UUID(created["sessionId"])
        self.assertNotEqual("thread-new", created["sessionId"])
        loaded = await self.handler.request(
            "session/load", {"sessionId": created["sessionId"], "cwd": "/other"}
        )
        self.assertEqual(created["sessionId"], loaded["sessionId"])
        self.assertEqual(
            [("thread-new", "/other", "le-wm", "feishu:one")],
            self.backend.loaded,
        )

    async def test_mapping_survives_handler_restart(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from agent_notifier.registry import SessionRegistry

        with TemporaryDirectory() as tmp:
            registry = SessionRegistry(Path(tmp) / "state.sqlite3")
            first = ACPHandler(
                self.backend,
                cwd="/workspace",
                project="le-wm",
                external_key="feishu:one",
                emit=self.handler.emit,
                registry=registry,
            )
            created = await first.request("session/new", {"cwd": "/workspace"})
            second = ACPHandler(
                self.backend,
                cwd="/workspace",
                project="le-wm",
                external_key="feishu:one",
                emit=self.handler.emit,
                registry=registry,
            )
            loaded = await second.request(
                "session/load", {"sessionId": created["sessionId"]}
            )
            self.assertEqual(created["sessionId"], loaded["sessionId"])
            self.assertEqual(
                [("thread-new", "/workspace", "le-wm", "feishu:one")],
                self.backend.loaded,
            )
            registry.close()

    async def test_prompt_forwards_final_text_to_cc_connect_history(self):
        await self.handler.request("session/load", {"sessionId": "thread-old"})
        result = await self.handler.request(
            "session/prompt",
            {"sessionId": "thread-old", "prompt": [{"type": "text", "text": "go"}]},
        )
        self.assertEqual("end_turn", result["stopReason"])
        chunks = [
            params["update"]["content"]["text"]
            for method, params in self.events
            if method == "session/update"
            and params["update"]["sessionUpdate"] == "agent_message_chunk"
        ]
        self.assertEqual(["hello ", "world"], chunks)
        self.assertEqual([("thread-old", "go", "cc_connect")], self.backend.prompts)

        updates = [params["update"] for method, params in self.events]
        self.assertEqual("agent_thought_chunk", updates[0]["sessionUpdate"])
        self.assertEqual("tool_call", updates[1]["sessionUpdate"])
        self.assertEqual("in_progress", updates[1]["status"])
        self.assertEqual("tool_call_update", updates[2]["sessionUpdate"])
        self.assertEqual("completed", updates[2]["status"])

    async def test_prompt_uses_active_target_but_keeps_cc_session_for_updates(self):
        await self.handler.request("session/load", {"sessionId": "thread-cc"})
        self.backend.active_thread = "thread-terminal"
        await self.handler.request(
            "session/prompt",
            {"sessionId": "thread-cc", "prompt": [{"type": "text", "text": "go"}]},
        )
        self.assertEqual(
            [("thread-terminal", "go", "cc_connect")],
            self.backend.prompts,
        )
        self.assertEqual(
            {"thread-cc"},
            {params["sessionId"] for _, params in self.events},
        )

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
