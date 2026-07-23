# Verification

## Verified versions

- Codex CLI: 0.145.0
- cc-connect: 1.3.2, commit `19406df9`
- Python: 3.10

## Completed checks

Run:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The 26-test suite covers session mapping, completion-origin policy, approval arbitration,
JSON-RPC routing, ACP translation, Codex turn start/steer selection, and a real
Unix WebSocket round trip through the proxy with a fake App Server.

Real local binaries were also exercised through an isolated service:

- Codex App Server `initialize` + `thread/list`: PASS.
- cc-connect 1.3.2 ACP `initialize` + `session/new`: PASS.
- Shared service remained alive for 60 seconds: PASS.
- Clean `uv` install, CLI status, and uninstall: PASS.
- Python compile check, shell syntax check, and `git diff --check`: PASS.

## Remaining real-environment checks

The following checks require the installed user service and a live cc-connect
Feishu project:

1. Launch native TUI with `agent-notifier codex`.
2. Attach a live Feishu project to the same Codex thread.
3. Send one prompt from each client.
4. Trigger one approval and verify first-responder-wins behavior.
