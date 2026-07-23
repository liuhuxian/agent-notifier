#!/usr/bin/env python3
"""Read-only smoke test against a running Agent Notifier service."""

import asyncio

from agent_notifier.codex.client import CodexAppServerClient
from agent_notifier.config import Paths


async def run() -> None:
    paths = Paths.from_environment()
    client = CodexAppServerClient(paths.proxy_socket, "smoke")
    await client.connect()
    try:
        result = await asyncio.wait_for(
            client.call("thread/list", {"limit": 1, "archived": False}), 10
        )
        if not isinstance(result, dict):
            raise RuntimeError(f"unexpected thread/list response: {result!r}")
        print("Real Codex App Server initialize + thread/list: PASS")
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(run())
