#!/usr/bin/env python3
"""Exercise a real Codex App Server turn through the notifier proxy."""

import argparse
import asyncio
from pathlib import Path

from agent_notifier.codex.client import CodexAppServerClient


async def run(socket_path: Path, cwd: Path) -> None:
    client = CodexAppServerClient(socket_path, "cc_connect")
    await client.connect()
    try:
        started = await client.call(
            "thread/start",
            {
                "cwd": str(cwd),
                "approvalPolicy": "on-request",
                "threadSource": "user",
            },
        )
        thread_id = started["thread"]["id"]
        result = await client.call(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": "Reply exactly: agent notifier prompt smoke",
                        "text_elements": [],
                    }
                ],
            },
        )
        turn_id = result["turn"]["id"]
        while True:
            event = await asyncio.wait_for(client.next_event(), timeout=120)
            method = event.get("method")
            params = event.get("params") or {}
            if method == "error":
                print(f"Codex error event: {params}")
            if method != "turn/completed":
                continue
            turn = params.get("turn") or {}
            if turn.get("id") != turn_id:
                continue
            if turn.get("status") != "completed":
                raise RuntimeError(f"turn did not complete: {turn}")
            print(f"Real Codex turn/start: PASS ({thread_id})")
            return
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--socket",
        type=Path,
        default=Path("/run/user/1003/agent-notifier/proxy.sock"),
    )
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    args = parser.parse_args()
    asyncio.run(run(args.socket, args.cwd))


if __name__ == "__main__":
    main()
