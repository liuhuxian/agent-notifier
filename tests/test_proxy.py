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
from agent_notifier.routing import ClientEventHub


class FakeUpstream:
    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.runner = None
        self.connections = []
        self.approval_responses = []

    async def start(self):
        app = web.Application()
        app.router.add_get("/", self.websocket)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.UnixSite(self.runner, str(self.socket_path)).start()

    async def websocket(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections.append(ws)
        async for message in ws:
            payload = json.loads(message.data)
            if "method" not in payload:
                self.approval_responses.append(payload)
                if payload.get("id") == 42:
                    await ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "method": "serverRequest/resolved",
                            "params": {
                                "threadId": "thread-1",
                                "requestId": 42,
                            },
                        }
                    )
                continue
            if "id" not in payload:
                continue
            if payload["method"] == "thread/resume":
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {
                            "thread": {
                                "id": payload["params"]["threadId"],
                            }
                        },
                    }
                )
                continue
            if payload["method"] == "turn/start":
                thread_id = payload["params"]["threadId"]
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "result": {"turn": {"id": "turn-remote"}},
                    }
                )
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/started",
                        "params": {
                            "threadId": thread_id,
                            "turn": {"id": "turn-remote", "status": "inProgress"},
                        },
                    }
                )
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/started",
                        "params": {
                            "threadId": thread_id,
                            "turnId": "turn-remote",
                            "item": {
                                "id": "user-remote",
                                "type": "userMessage",
                                "content": [{"type": "text", "text": "remote task"}],
                            },
                        },
                    }
                )
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

    async def send_approval(self, connection_index=0):
        for _ in range(20):
            if len(self.connections) > connection_index:
                break
            await asyncio.sleep(0.01)
        if len(self.connections) <= connection_index:
            raise RuntimeError(
                f"upstream connection {connection_index} is unavailable"
            )
        await self.connections[connection_index].send_json(
            {
                "jsonrpc": "2.0",
                "id": 42,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "command-1",
                    "command": "touch /tmp/test",
                },
            }
        )

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


class ClientEventHubTest(unittest.IsolatedAsyncioTestCase):
    async def test_notification_is_scoped_to_subscribed_thread(self):
        hub = ClientEventHub()
        first = RecordingSocket()
        second = RecordingSocket()
        await hub.add_client("terminal:first", first)
        await hub.add_client("terminal:second", second)
        await hub.subscribe("terminal:first", "thread-1")
        await hub.subscribe("terminal:second", "thread-2")

        await hub.route_notification(
            {
                "method": "turn/started",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1"},
                },
            },
            "terminal:first",
        )

        self.assertEqual(1, len(first.messages))
        self.assertEqual([], second.messages)

    async def test_duplicate_turn_stream_from_second_upstream_is_suppressed(self):
        hub = ClientEventHub()
        terminal = RecordingSocket()
        remote = RecordingSocket()
        await hub.add_client("terminal", terminal)
        await hub.add_client("remote", remote)
        await hub.subscribe("terminal", "thread-1")
        await hub.subscribe("remote", "thread-1")
        event = {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "delta": "same chunk",
            },
        }

        self.assertTrue(await hub.route_notification(event, "remote"))
        self.assertFalse(await hub.route_notification(event, "terminal"))

        self.assertEqual(1, len(terminal.messages))
        self.assertEqual(1, len(remote.messages))

    async def test_remaining_client_takes_over_stream_after_source_disconnects(self):
        hub = ClientEventHub()
        terminal = RecordingSocket()
        remote = RecordingSocket()
        await hub.add_client("terminal", terminal)
        await hub.add_client("remote", remote)
        await hub.subscribe("terminal", "thread-1")
        await hub.subscribe("remote", "thread-1")
        first = {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "delta": "before",
            },
        }
        second = {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "delta": "after",
            },
        }

        self.assertTrue(await hub.route_notification(first, "terminal"))
        await hub.remove_client("terminal")
        self.assertTrue(await hub.route_notification(second, "remote"))
        self.assertEqual(["before", "after"], [
            message["params"]["delta"] for message in remote.messages
        ])

    async def test_registered_turn_owner_wins_before_first_event_arrives(self):
        hub = ClientEventHub()
        terminal = RecordingSocket()
        cc_connect = RecordingSocket()
        await hub.add_client("terminal:one", terminal)
        await hub.add_client("cc_connect:one", cc_connect)
        await hub.subscribe("terminal:one", "thread-1")
        await hub.subscribe("cc_connect:one", "thread-1")
        await hub.register_turn_request(
            "thread-1", "cc_connect:one", "turn/start"
        )
        event = {
            "method": "turn/started",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1"},
            },
        }

        self.assertFalse(
            await hub.route_notification(event, "terminal:one")
        )
        self.assertTrue(
            await hub.route_notification(event, "cc_connect:one")
        )
        completed = {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        }
        self.assertFalse(
            await hub.route_notification(completed, "terminal:one")
        )
        self.assertTrue(
            await hub.route_notification(completed, "cc_connect:one")
        )
        self.assertEqual([event, completed], terminal.messages)
        self.assertEqual([event, completed], cc_connect.messages)


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

    async def test_external_approval_rewrites_resolved_request_for_terminal(self):
        terminal = await self.session.ws_connect("http://localhost/?client=terminal")
        try:
            await self.upstream.send_approval()
            approval = await terminal.receive_json()
            token = approval["id"]

            async with self.session.post(
                "http://localhost/approval",
                json={
                    "approval_id": token.rsplit(":", 1)[-1][:10],
                    "decision": "allow",
                },
            ) as response:
                self.assertEqual(200, response.status)

            resolved = await terminal.receive_json()
            self.assertEqual("serverRequest/resolved", resolved["method"])
            self.assertEqual(token, resolved["params"]["requestId"])
        finally:
            await terminal.close()

    async def test_cc_connect_approval_is_hidden_from_source_but_reaches_terminal(self):
        notifications = []

        async def notify(token, payload, origin):
            notifications.append((token, payload, origin))

        self.proxy.on_approval_request = notify
        remote = await self.session.ws_connect(
            "http://localhost/?client=cc_connect"
        )
        terminal = await self.session.ws_connect(
            "http://localhost/?client=terminal"
        )
        try:
            for request_id, socket in ((30, remote), (31, terminal)):
                await socket.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "thread/resume",
                        "params": {"threadId": "thread-1"},
                    }
                )
                self.assertEqual(
                    request_id, (await socket.receive_json())["id"]
                )

            await remote.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 32,
                    "method": "turn/start",
                    "params": {
                        "threadId": "thread-1",
                        "input": [{"type": "text", "text": "remote task"}],
                    },
                }
            )
            self.assertEqual(32, (await remote.receive_json())["id"])
            for _ in range(2):
                await remote.receive_json()
                await terminal.receive_json()

            await self.upstream.send_approval(connection_index=0)
            await self.upstream.send_approval(connection_index=1)

            approval = await asyncio.wait_for(
                terminal.receive_json(), timeout=0.2
            )
            self.assertEqual(
                "item/commandExecution/requestApproval",
                approval["method"],
            )
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(remote.receive_json(), timeout=0.1)
            self.assertEqual(1, len(notifications))
            self.assertEqual(approval["id"], notifications[0][0])
            self.assertEqual("cc_connect", notifications[0][2])
        finally:
            await terminal.close()
            await remote.close()

    async def test_remote_turn_events_reach_terminal_on_same_thread(self):
        terminal = await self.session.ws_connect("http://localhost/?client=terminal")
        remote = await self.session.ws_connect("http://localhost/?client=cc_connect")
        try:
            await terminal.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 20,
                    "method": "thread/resume",
                    "params": {"threadId": "thread-1"},
                }
            )
            self.assertEqual(20, (await terminal.receive_json())["id"])
            await remote.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 21,
                    "method": "thread/resume",
                    "params": {"threadId": "thread-1"},
                }
            )
            self.assertEqual(21, (await remote.receive_json())["id"])

            await remote.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": 22,
                    "method": "turn/start",
                    "params": {
                        "threadId": "thread-1",
                        "input": [{"type": "text", "text": "remote task"}],
                    },
                }
            )
            self.assertEqual(22, (await remote.receive_json())["id"])
            try:
                started = await asyncio.wait_for(
                    terminal.receive_json(), timeout=0.2
                )
            except asyncio.TimeoutError:
                self.fail("terminal did not receive the remote turn notification")
            self.assertEqual("turn/started", started["method"])
            self.assertEqual("thread-1", started["params"]["threadId"])

            item = await terminal.receive_json()
            self.assertEqual("item/started", item["method"])
            self.assertEqual(
                "remote task", item["params"]["item"]["content"][0]["text"]
            )
        finally:
            await remote.close()
            await terminal.close()

    async def test_completion_uses_exactly_one_callback_for_each_origin(self):
        terminal_notifications = []
        remote_notifications = []
        remote_progress = []

        async def notify_terminal(thread_id, text):
            terminal_notifications.append((thread_id, text))

        async def notify_remote(thread_id, text):
            remote_notifications.append((thread_id, text))

        async def notify_progress(thread_id, text):
            remote_progress.append((thread_id, text))

        self.proxy.on_terminal_completion = notify_terminal
        self.proxy.on_remote_completion = notify_remote
        self.proxy.on_remote_progress = notify_progress
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
        commentary = {
            "method": "item/completed",
            "params": {
                "threadId": "thread-cc",
                "turnId": "turn-2",
                "item": {
                    "id": "commentary-2",
                    "type": "agentMessage",
                    "text": "remote progress",
                    "phase": "commentary",
                },
            },
        }
        await self.proxy._observe_notification(commentary)
        await self.proxy._observe_notification(commentary)
        await self.proxy._observe_notification(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-cc",
                    "turnId": "turn-2",
                    "item": {
                        "id": "message-2",
                        "type": "agentMessage",
                        "text": "remote result",
                        "phase": "final_answer",
                    },
                },
            }
        )
        await self.proxy._observe_notification(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-cc",
                    "turn": {"id": "turn-2", "status": "completed"},
                },
            }
        )
        self.assertEqual(
            [("thread-terminal", "done")], terminal_notifications
        )
        self.assertEqual(
            [("thread-cc", "remote result")], remote_notifications
        )
        self.assertEqual(
            [("thread-cc", "remote progress")], remote_progress
        )

    async def test_token_usage_notification_calls_persistence_callback(self):
        notifications = []

        async def record(thread_id, token_usage):
            notifications.append((thread_id, token_usage))

        self.proxy.on_token_usage = record
        usage = {
            "total": {"totalTokens": 120},
            "last": {"totalTokens": 20},
            "modelContextWindow": 1000,
        }
        await self.proxy._observe_notification(
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread-usage",
                    "turnId": "turn-usage",
                    "tokenUsage": usage,
                },
            }
        )

        self.assertEqual([("thread-usage", usage)], notifications)

    async def test_terminal_completion_uses_only_last_final_answer(self):
        notifications = []

        async def notify(thread_id, text):
            notifications.append((thread_id, text))

        self.proxy.on_terminal_completion = notify
        self.proxy._thread_origins["thread-terminal"] = "terminal"
        messages = [
            ("commentary-1", "commentary"),
            ("commentary-2", "commentary"),
            ("final response", "final_answer"),
        ]
        for index, (text, phase) in enumerate(messages, start=1):
            item_id = f"message-{index}"
            await self.proxy._observe_notification(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "thread-terminal",
                        "turnId": "turn-1",
                        "itemId": item_id,
                        "delta": text,
                    },
                }
            )
            await self.proxy._observe_notification(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-terminal",
                        "turnId": "turn-1",
                        "completedAtMs": index,
                        "item": {
                            "id": item_id,
                            "type": "agentMessage",
                            "text": text,
                            "phase": phase,
                        },
                    },
                }
            )

        await self.proxy._observe_notification(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-terminal",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            }
        )

        self.assertEqual(
            [("thread-terminal", "final response")],
            notifications,
        )


if __name__ == "__main__":
    unittest.main()
