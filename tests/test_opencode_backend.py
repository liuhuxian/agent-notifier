"""Tests for the opencode backend."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from agent_notifier.config import ProgressConfig
from agent_notifier.opencode.backend import OpencodeBackend
from agent_notifier.registry import SessionRegistry


class FakeOpencodeClient:
    def __init__(self):
        self.created_sessions = []
        self.prompts = []
        self.permission_replies = []
        self.events = asyncio.Queue()
        self.sse_started = False

    async def connect(self):
        pass

    async def start_sse(self):
        self.sse_started = True

    async def next_event(self):
        return await self.events.get()

    async def create_session(self):
        sid = f"ses_test_{len(self.created_sessions)}"
        self.created_sessions.append(sid)
        return {"id": sid}

    async def get_sessions(self):
        return [{"id": s} for s in self.created_sessions]

    async def send_prompt(self, session_id, text):
        self.prompts.append((session_id, text))
        return {"info": {"id": "msg_test"}}

    async def reply_permission(self, session_id, permission_id, response):
        self.permission_replies.append((session_id, permission_id, response))
        return {}

    async def get_messages(self, session_id, limit=50):
        return []

    async def close(self):
        pass


class OpencodeBackendTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = SessionRegistry(Path(self.tmp.name) / "state.sqlite3")
        self.client = FakeOpencodeClient()
        self.permission_calls = []
        progress = ProgressConfig(stream_preview=True)
        self.backend = OpencodeBackend(
            self.client,
            self.registry,
            self._fake_request_permission,
            progress=progress,
        )

    async def _fake_request_permission(self, params):
        self.permission_calls.append(params)
        return {"outcome": {"outcome": "selected", "optionId": "allow_once"}}

    async def asyncTearDown(self):
        await self.backend.close()
        self.registry.close()
        self.tmp.cleanup()

    async def test_start_thread_creates_session(self):
        session_id = await self.backend.start_thread(
            "/workspace", "le-wm", "feishu:one"
        )
        self.assertTrue(session_id.startswith("ses_test_"))
        mapping = self.registry.get("cc_connect", "le-wm", "feishu:one")
        self.assertEqual(session_id, mapping.thread_id)

    async def test_resume_thread_uses_registry(self):
        self.registry.bind(
            "cc_connect", "le-wm", "feishu:one", "ses_existing", "/workspace"
        )
        session_id = await self.backend.resume_thread(
            "ses_unknown", "/workspace", "le-wm", "feishu:one"
        )
        self.assertEqual("ses_existing", session_id)

    async def test_resume_thread_falls_back_to_api(self):
        self.client.created_sessions = ["ses_abc123"]
        session_id = await self.backend.resume_thread(
            "ses_abc", "/workspace", "le-wm", "feishu:one"
        )
        self.assertEqual("ses_abc123", session_id)

    async def test_prompt_returns_end_turn_on_idle(self):
        session_id = "ses_test_prompt"
        emit_calls = []

        async def emit(event):
            emit_calls.append(event)

        task = asyncio.create_task(
            self.backend.prompt(session_id, "hello", "cc_connect", emit)
        )
        await asyncio.sleep(0.05)
        await self.client.events.put({
            "type": "session.idle",
            "properties": {"sessionID": session_id},
        })
        result = await asyncio.wait_for(task, timeout=2)
        self.assertEqual("end_turn", result.get("stopReason"))
        self.assertIn(("ses_test_prompt", "hello"), self.client.prompts)

    async def test_prompt_streams_delta(self):
        session_id = "ses_test_delta"
        emit_calls = []

        async def emit(event):
            emit_calls.append(event)

        task = asyncio.create_task(
            self.backend.prompt(session_id, "test", "cc_connect", emit)
        )
        await asyncio.sleep(0.05)
        # First, register a text part
        await self.client.events.put({
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {"id": "prt_1", "type": "text", "text": ""},
            },
        })
        await self.client.events.put({
            "type": "message.part.delta",
            "properties": {
                "sessionID": session_id,
                "partID": "prt_1",
                "field": "text",
                "delta": "Hello ",
            },
        })
        await self.client.events.put({
            "type": "message.part.delta",
            "properties": {
                "sessionID": session_id,
                "partID": "prt_1",
                "field": "text",
                "delta": "World",
            },
        })
        await self.client.events.put({
            "type": "session.idle",
            "properties": {"sessionID": session_id},
        })
        await asyncio.wait_for(task, timeout=2)
        self.assertIn(
            {"kind": "text", "text": "Hello "},
            emit_calls,
        )
        self.assertIn(
            {"kind": "text", "text": "World"},
            emit_calls,
        )

    async def test_prompt_ignores_reasoning_delta(self):
        session_id = "ses_test_reason"
        emit_calls = []

        async def emit(event):
            emit_calls.append(event)

        task = asyncio.create_task(
            self.backend.prompt(session_id, "test", "cc_connect", emit)
        )
        await asyncio.sleep(0.05)
        await self.client.events.put({
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {"id": "prt_reason", "type": "reasoning", "text": ""},
            },
        })
        await self.client.events.put({
            "type": "message.part.delta",
            "properties": {
                "sessionID": session_id,
                "partID": "prt_reason",
                "field": "text",
                "delta": "reasoning content",
            },
        })
        await self.client.events.put({
            "type": "session.idle",
            "properties": {"sessionID": session_id},
        })
        await asyncio.wait_for(task, timeout=2)
        text_emits = [e for e in emit_calls if e.get("kind") == "text"]
        self.assertEqual(0, len(text_emits))

    async def test_prompt_handles_permission(self):
        session_id = "ses_test_perm"
        emit_calls = []

        async def emit(event):
            emit_calls.append(event)

        task = asyncio.create_task(
            self.backend.prompt(session_id, "test", "cc_connect", emit)
        )
        await asyncio.sleep(0.05)
        await self.client.events.put({
            "type": "permission.asked",
            "properties": {
                "id": "perm_001",
                "sessionID": session_id,
                "permission": "write",
                "metadata": {"filepath": "/tmp/test.txt"},
            },
        })
        await self.client.events.put({
            "type": "session.idle",
            "properties": {"sessionID": session_id},
        })
        await asyncio.wait_for(task, timeout=2)
        self.assertIn(
            ("ses_test_perm", "perm_001", "once"),
            self.client.permission_replies,
        )

    async def test_prompt_raises_on_error(self):
        session_id = "ses_test_error"

        async def emit(event):
            pass

        task = asyncio.create_task(
            self.backend.prompt(session_id, "test", "cc_connect", emit)
        )
        await asyncio.sleep(0.05)
        await self.client.events.put({
            "type": "session.error",
            "properties": {
                "sessionID": session_id,
                "error": "something went wrong",
            },
        })
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(task, timeout=2)

    async def test_start_thread_binds_registry(self):
        session_id = await self.backend.start_thread(
            "/workspace", "proj", "key"
        )
        mapping = self.registry.get("cc_connect", "proj", "key")
        self.assertEqual(session_id, mapping.thread_id)
        self.assertEqual("/workspace", mapping.cwd)