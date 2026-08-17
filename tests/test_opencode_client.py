"""Tests for OpenCode HTTP/SSE reconnect behavior."""

import asyncio
import unittest

from agent_notifier.opencode.client import OpencodeClient


class _Content:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_any(self):
        for chunk in self.chunks:
            yield chunk


class _Response:
    def __init__(self, chunks=(), error=None):
        self.content = _Content(chunks)
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self):
        self.get_calls = 0
        self.get_kwargs = []

    def get(self, *args, **kwargs):
        self.get_calls += 1
        self.get_kwargs.append(kwargs)
        if self.get_calls == 1:
            return _Response(error=ConnectionError("server restarted"))
        return _Response([b'data: {"type":"server.connected"}\n\n'])

    async def close(self):
        pass


class OpencodeClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_sse_reconnects_after_transport_error(self):
        client = OpencodeClient(
            "http://opencode.test", directory="/users/huxian/project/le-wm"
        )
        client._session = _FakeSession()
        await client.start_sse()
        try:
            event = await asyncio.wait_for(client.next_event(), 1)
            self.assertEqual("_error", event["type"])
            connected = await asyncio.wait_for(client.next_event(), 1)
            self.assertEqual("server.connected", connected["type"])
            self.assertGreaterEqual(client._session.get_calls, 2)
            self.assertTrue(all(
                call.get("params") == {
                    "directory": "/users/huxian/project/le-wm"
                }
                for call in client._session.get_kwargs
            ))
        finally:
            await client.close()
