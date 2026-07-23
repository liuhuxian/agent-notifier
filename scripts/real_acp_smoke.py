#!/usr/bin/env python3
"""Exercise the exact ACP calls used by cc-connect 1.3.2."""

import json
import os
import subprocess
import sys
from pathlib import Path


def request(process: subprocess.Popen, request_id: int, method: str, params: dict) -> dict:
    process.stdin.write(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        + "\n"
    )
    process.stdin.flush()
    while True:
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("ACP process closed unexpectedly")
        payload = json.loads(line)
        if payload.get("id") == request_id:
            if "error" in payload:
                raise RuntimeError(payload["error"])
            return payload["result"]


def main() -> None:
    env = os.environ.copy()
    env.update(
        {
            "CC_PROJECT": "agent-notifier-smoke",
            "CC_SESSION_KEY": "smoke:local",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_notifier.cli", "acp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(Path.cwd()),
    )
    try:
        initialized = request(
            process,
            1,
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "cc-connect", "version": "1.3.2"},
            },
        )
        if not initialized["agentCapabilities"]["loadSession"]:
            raise RuntimeError("loadSession was not advertised")
        created = request(
            process,
            2,
            "session/new",
            {"cwd": str(Path.cwd()), "mcpServers": []},
        )
        thread_id = created.get("sessionId")
        if not thread_id:
            raise RuntimeError("session/new returned no sessionId")
        print(f"Real cc-connect 1.3.2 ACP initialize + session/new: PASS ({thread_id})")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    main()
