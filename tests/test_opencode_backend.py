"""Tests for the opencode backend."""

import asyncio
import json
import socket
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

    async def test_tool_lifecycle_socket_emits_acp_events(self):
        socket_path = Path(self.tmp.name) / "opencode-tools.sock"
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            probe.bind(str(socket_path))
        except PermissionError:
            probe.close()
            self.skipTest("sandbox does not allow Unix datagram sockets")
        finally:
            probe.close()
            socket_path.unlink(missing_ok=True)
        backend = OpencodeBackend(
            self.client,
            self.registry,
            self._fake_request_permission,
            progress=ProgressConfig(),
            tool_event_socket=socket_path,
        )
        events = []

        async def emit(event):
            events.append(event)

        backend._active_emitters["ses_tool"] = emit
        backend._ensure_tool_event_listener()
        for _ in range(50):
            if socket_path.exists():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(socket_path.exists())
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            for payload in (
                {
                    "phase": "start",
                    "sessionID": "ses_tool",
                    "callID": "call-1",
                    "tool": "bash",
                    "args": {"command": "echo hi"},
                },
                {
                    "phase": "complete",
                    "sessionID": "ses_tool",
                    "callID": "call-1",
                },
            ):
                sender.sendto(json.dumps(payload).encode(), str(socket_path))
        for _ in range(50):
            if len(events) == 2:
                break
            await asyncio.sleep(0.01)
        self.assertEqual("tool_start", events[0]["kind"])
        self.assertEqual("call-1", events[0]["tool_call_id"])
        self.assertEqual("tool_complete", events[1]["kind"])
        await backend.close()

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

    async def test_prompt_emits_heartbeat_during_silent_turn(self):
        session_id = "ses_test_heartbeat"
        emit_calls = []
        self.backend.progress = ProgressConfig(
            stream_preview=True, heartbeat_interval_ms=1000
        )

        async def emit(event):
            emit_calls.append(event)

        task = asyncio.create_task(
            self.backend.prompt(session_id, "long task", "cc_connect", emit)
        )
        await asyncio.sleep(1.1)
        await self.client.events.put({
            "type": "session.idle",
            "properties": {"sessionID": session_id},
        })
        await asyncio.wait_for(task, timeout=2)
        self.assertTrue(
            any(
                event.get("kind") == "status"
                and event.get("text") == "仍在执行"
                for event in emit_calls
            )
        )

    async def test_prompt_streams_delta(self):
        session_id = "ses_test_delta"
        emit_calls = []

        async def emit(event):
            emit_calls.append(event)

        task = asyncio.create_task(
            self.backend.prompt(session_id, "test", "cc_connect", emit)
        )
        await asyncio.sleep(0.05)
        # Register assistant message
        await self.client.events.put({
            "type": "message.updated",
            "properties": {
                "sessionID": session_id,
                "info": {"id": "msg_asst", "role": "assistant"},
            },
        })
        # Register a text part belonging to the assistant message
        await self.client.events.put({
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {"id": "prt_1", "messageID": "msg_asst", "type": "text", "text": ""},
            },
        })
        await self.client.events.put({
            "type": "message.part.delta",
            "properties": {
                "sessionID": session_id,
                "messageID": "msg_asst",
                "partID": "prt_1",
                "field": "text",
                "delta": "Hello ",
            },
        })
        await self.client.events.put({
            "type": "message.part.delta",
            "properties": {
                "sessionID": session_id,
                "messageID": "msg_asst",
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
            "type": "message.updated",
            "properties": {
                "sessionID": session_id,
                "info": {"id": "msg_asst", "role": "assistant"},
            },
        })
        await self.client.events.put({
            "type": "message.part.updated",
            "properties": {
                "sessionID": session_id,
                "part": {"id": "prt_reason", "messageID": "msg_asst", "type": "reasoning", "text": ""},
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
                "always": ["/tmp/test.txt"],
            },
        })
        await self.client.events.put({
            "type": "session.idle",
            "properties": {"sessionID": session_id},
        })
        await asyncio.wait_for(task, timeout=2)
        self.assertEqual("end_turn", task.result().get("stopReason"))
        self.assertEqual(
            [(session_id, "perm_001", "once")],
            self.client.permission_replies,
        )
        self.assertEqual(
            {"allow_once", "allow_always", "deny_once"},
            {
                option["optionId"]
                for option in self.permission_calls[0]["options"]
            },
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
