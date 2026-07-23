# Agent Notifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone user-level service that lets the native Codex TUI
and cc-connect share Codex sessions while providing reliable notifications and
remote approval handling.

**Architecture:** A Python asyncio daemon supervises Codex App Server and exposes
a local JSON-RPC proxy. A session registry and origin policy sit above the proxy;
an ACP stdio adapter connects cc-connect without modifying cc-connect itself.

**Tech Stack:** Python 3.10+, asyncio, aiohttp, SQLite, TOML, pytest, systemd user
services, Codex App Server JSON-RPC, Agent Client Protocol.

---

### Task 1: Package Skeleton And Compatibility

**Files:**
- Create: `pyproject.toml`
- Create: `src/agent_notifier/versioning.py`
- Create: `src/agent_notifier/config.py`
- Test: `tests/test_versioning.py`
- Test: `tests/test_config.py`

- [ ] Write tests for semantic compatibility checks and XDG defaults.
- [ ] Run `pytest tests/test_versioning.py tests/test_config.py -v` and verify failure.
- [ ] Implement package metadata, compatibility constants, and typed config.
- [ ] Run the tests and verify they pass.

### Task 2: Persistent Session Registry And Policies

**Files:**
- Create: `src/agent_notifier/registry.py`
- Create: `src/agent_notifier/policy.py`
- Test: `tests/test_registry.py`
- Test: `tests/test_policy.py`

- [ ] Write tests for atomic external-session mapping and origin notification rules.
- [ ] Verify the tests fail before implementation.
- [ ] Implement SQLite transactions and pure policy functions.
- [ ] Verify concurrent lookup/create produces one mapping.

### Task 3: JSON-RPC Routing And Approval Arbitration

**Files:**
- Create: `src/agent_notifier/jsonrpc.py`
- Create: `src/agent_notifier/approvals.py`
- Test: `tests/test_jsonrpc.py`
- Test: `tests/test_approvals.py`

- [ ] Write tests for request-ID rewriting, response restoration, and first-writer wins.
- [ ] Implement routing tables with cleanup on disconnect.
- [ ] Implement persisted idempotent approval resolution.
- [ ] Run focused tests and verify all pass.

### Task 4: Codex App Server Client And Supervisor

**Files:**
- Create: `src/agent_notifier/codex/client.py`
- Create: `src/agent_notifier/codex/supervisor.py`
- Create: `src/agent_notifier/proxy.py`
- Test: `tests/fakes/fake_codex_server.py`
- Test: `tests/test_codex_integration.py`

- [ ] Create a fake newline JSON-RPC/WebSocket server for deterministic tests.
- [ ] Test initialize, thread start/resume, turn start/steer, and approval routing.
- [ ] Implement bounded restart and explicit in-flight failure.
- [ ] Verify two clients can attach to one fake thread.

### Task 5: cc-connect ACP Adapter

**Files:**
- Create: `src/agent_notifier/acp/protocol.py`
- Create: `src/agent_notifier/acp/server.py`
- Test: `tests/test_acp_integration.py`

- [ ] Capture the cc-connect 1.3.2 ACP handshake and required methods in fixtures.
- [ ] Write a stdio integration test for session creation, prompt, stream, and permission.
- [ ] Implement only the ACP capabilities required by cc-connect 1.3.2.
- [ ] Verify malformed messages fail with structured errors.

### Task 6: CLI, Service, Installer, And Documentation

**Files:**
- Create: `src/agent_notifier/cli.py`
- Create: `systemd/agent-notifier.service`
- Create: `install.sh`
- Create: `README.md`
- Create: `tests/test_cli.py`
- Create: `tests/test_installer.py`

- [ ] Test CLI status/doctor output and reversible config transformation.
- [ ] Implement `codex`, `serve`, `acp`, `status`, `doctor`, `logs`, and `uninstall`.
- [ ] Implement user-systemd install with background-process fallback.
- [ ] Document Codex 0.145.0 and cc-connect 1.3.2 compatibility explicitly.
- [ ] Verify install/uninstall against temporary HOME and XDG directories.

### Task 7: End-To-End Verification

**Files:**
- Create: `scripts/smoke_test.sh`
- Create: `docs/verification.md`

- [ ] Run the complete unit and fake-server integration suite.
- [ ] Install into an isolated temporary home and run `agent-notifier doctor`.
- [ ] Run the service for at least one minute and verify health/log output.
- [ ] Run a real Codex 0.145.0 remote-TUI smoke test.
- [ ] Record cc-connect 1.3.2 ACP evidence or any remaining external prerequisite.
- [ ] Record exact commands and outputs in `docs/verification.md`.
