import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, UnixConnector, web

from agent_notifier.proxy import AppServerProxy
from agent_notifier.proxy import ApprovalFanout
from agent_notifier.approvals import ApprovalStore
from agent_notifier.codex.client import CodexAppServerClient


class FakeUpstream:
    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.runner = None

    async def start(self):
        app = web.Application()
        app.router.add_get("/", self.websocket)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.UnixSite(self.runner, str(self.socket_path)).start()

    async def websocket(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            payload = json.loads(message.data)
            if "id" not in payload:
                continue
            if payload["method"] == "test/large":
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {"blob": "x" * (4 * 1024 * 1024 + 1)},
                    }
                )
                continue
            await ws.send_json(
                {"jsonrpc": "2.0", "id": payload["id"], "result": {"method": payload["method"]}}
            )
        return ws

    async def close(self):
        if self.runner:
            await self.runner.cleanup()


class RecordingSocket:
    def __init__(self):
        self.closed = False
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


class ApprovalFanoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_winning_response_is_persisted_and_forwarded_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ApprovalStore(Path(tmp) / "state.sqlite3")
            fanout = ApprovalFanout(store)
            terminal = RecordingSocket()
            cc_connect = RecordingSocket()
            upstream = RecordingSocket()
            await fanout.add_client("terminal", terminal)
            await fanout.add_client("cc_connect", cc_connect)
            await fanout.publish(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": "thread-1"},
                },
                upstream,
            )
            token = terminal.messages[0]["id"]
            self.assertEqual(token, cc_connect.messages[0]["id"])
            response = {"jsonrpc": "2.0", "id": token, "result": {"decision": "accept"}}
            self.assertTrue(await fanout.resolve(response, "terminal"))
            self.assertTrue(await fanout.resolve(response, "cc_connect"))
            self.assertEqual(1, len(upstream.messages))
            self.assertEqual(9, upstream.messages[0]["id"])
            resolution = store.get_resolution(token)
            self.assertEqual("allow", resolution.decision)
            self.assertEqual("terminal", resolution.resolved_by)
            store.close()

    async def test_external_short_id_resolves_pending_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ApprovalStore(Path(tmp) / "state.sqlite3")
            fanout = ApprovalFanout(store)
            upstream = RecordingSocket()
            token = await fanout.publish(
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"threadId": "thread-1", "command": "touch /tmp/x"},
                },
                upstream,
            )
            short_id = token.rsplit(":", 1)[-1][:10]
            self.assertEqual(
                "resolved",
                await fanout.resolve_external(short_id, "allow"),
            )
            self.assertEqual(
                {"decision": "accept"}, upstream.messages[0]["result"]
            )
            self.assertEqual(
                "already_resolved",
                await fanout.resolve_external(short_id, "deny"),
            )
            self.assertEqual(1, len(upstream.messages))
            resolution = store.get_resolution(token)
            self.assertEqual("allow", resolution.decision)
            self.assertEqual("feishu", resolution.resolved_by)
            store.close()

    async def test_external_permission_denial_uses_permission_result_shape(self):
        fanout = ApprovalFanout()
        upstream = RecordingSocket()
        token = await fanout.publish(
            {
                "jsonrpc": "2.0",
                "id": 12,
                "method": "item/permissions/requestApproval",
                "params": {
                    "threadId": "thread-1",
                    "permissions": {"network": True},
                },
            },
            upstream,
        )
        await fanout.resolve_external(token, "deny")
        self.assertEqual(
            {"permissions": {}, "scope": "turn"},
            upstream.messages[0]["result"],
        )


class ProxyIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.upstream = FakeUpstream(root / "upstream.sock")
        await self.upstream.start()
        self.proxy = AppServerProxy(root / "proxy.sock", root / "upstream.sock")
        await self.proxy.start()
        self.connector = UnixConnector(path=str(root / "proxy.sock"))
        self.session = ClientSession(connector=self.connector)

    async def asyncTearDown(self):
        await self.session.close()
        await self.proxy.close()
        await self.upstream.close()
        self.tmp.cleanup()

    async def test_round_trip_preserves_request(self):
        ws = await self.session.ws_connect("http://localhost/")
        await ws.send_json({"jsonrpc": "2.0", "id": 7, "method": "thread/read", "params": {}})
        response = await ws.receive_json()
        self.assertEqual(7, response["id"])
        self.assertEqual("thread/read", response["result"]["method"])
        await ws.close()

    async def test_native_tui_rpc_path_preserves_request(self):
        ws = await self.session.ws_connect("http://localhost/rpc")
        await ws.send_json({"jsonrpc": "2.0", "id": 8, "method": "thread/read", "params": {}})
        response = await ws.receive_json()
        self.assertEqual(8, response["id"])
        self.assertEqual("thread/read", response["result"]["method"])
        await ws.close()

    async def test_large_resume_response_is_preserved(self):
        ws = await self.session.ws_connect("http://localhost/rpc", max_msg_size=0)
        await ws.send_json({"jsonrpc": "2.0", "id": 9, "method": "test/large", "params": {}})
        response = await ws.receive_json()
        self.assertEqual(9, response["id"])
        self.assertEqual(4 * 1024 * 1024 + 1, len(response["result"]["blob"]))
        await ws.close()

    async def test_codex_client_initializes_through_proxy(self):
        client = CodexAppServerClient(self.proxy.listen_socket, "cc_connect")
        await client.connect()
        result = await client.call("thread/read", {})
        self.assertEqual("thread/read", result["method"])
        await client.close()

    async def test_http_approval_control_resolves_pending_request(self):
        upstream = RecordingSocket()
        token = await self.proxy.fanout.publish(
            {
                "jsonrpc": "2.0",
                "id": 13,
                "method": "item/fileChange/requestApproval",
                "params": {"threadId": "thread-1", "grantRoot": "/workspace"},
            },
            upstream,
        )
        async with self.session.post(
            "http://localhost/approval",
            json={
                "approval_id": token.rsplit(":", 1)[-1][:10],
                "decision": "allow",
            },
        ) as response:
            self.assertEqual(200, response.status)
            self.assertEqual("resolved", (await response.json())["status"])
        self.assertEqual({"decision": "accept"}, upstream.messages[0]["result"])

    async def test_terminal_completion_notifies_once_but_cc_completion_does_not(self):
        notifications = []

        async def notify(thread_id, text):
            notifications.append((thread_id, text))

        self.proxy.on_terminal_completion = notify
        self.proxy._thread_origins["thread-terminal"] = "terminal"
        await self.proxy._observe_notification(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-terminal",
                    "turnId": "turn-1",
                    "delta": "done",
                },
            }
        )
        completed = {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-terminal",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        }
        await self.proxy._observe_notification(completed)
        await self.proxy._observe_notification(completed)

        self.proxy._thread_origins["thread-cc"] = "cc_connect"
        await self.proxy._observe_notification(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-cc",
                    "turn": {"id": "turn-2", "status": "completed"},
                },
            },
            "cc_connect",
        )
        self.assertEqual([("thread-terminal", "done")], notifications)


if __name__ == "__main__":
    unittest.main()
