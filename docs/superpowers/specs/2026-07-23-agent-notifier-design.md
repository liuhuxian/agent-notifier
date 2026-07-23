# Agent Notifier Design

## Goal

Provide reliable completion notifications, remote approvals, and continued
conversation from Feishu while preserving the full native terminal experience
of supported coding agents.

The first release supports Codex CLI 0.145.0 and cc-connect 1.3.2. The core is
agent-neutral so OpenCode can be added as a later adapter.

## Clarified Decisions

- The project is a standalone Git repository at `3d_party/agent-notifier`.
- It is installed per user and supports multiple projects and working directories.
- The normal `codex` command remains unchanged. `agent-notifier codex` launches
  the complete Codex TUI against the shared service.
- A shared Codex App Server is the source of truth for threads and turns.
- Terminal and Feishu clients can attach to the same thread.
- Messages arriving during an active steerable turn use `turn/steer`.
- Approval requests are visible to both clients; the first valid response wins.
- Feishu-originated turns do not generate a redundant completion notification.
- cc-connect is integrated through its supported ACP extension point. Its source
  and binary are not forked or patched.
- Linux uses a user-level systemd service. Other systems fall back to a managed
  background process.
- Repository files contain no credentials or runtime session state.

## Architecture

```text
 native Codex TUI                         Feishu
       |                                    |
       | remote App Server protocol         | platform transport
       v                                    v
+----------------------+              +------------+
| agent-notifier proxy |<------------>| cc-connect |
|                      |   ACP stdio   +------------+
| session mapping      |
| origin tracking      |
| approval arbitration |
+----------+-----------+
           |
           | Codex App Server protocol
           v
+----------------------+
| shared Codex         |
| App Server           |
+----------------------+
```

## Components

### Supervisor

Starts one Codex App Server and one proxy per user. It uses Unix sockets under
`$XDG_RUNTIME_DIR/agent-notifier` and a lock file to prevent duplicate owners.
It records child process health and restarts the App Server with bounded
backoff. Runtime state is never stored in the source repository.

### Protocol Proxy

The proxy gives every downstream client its own App Server connection, so normal
JSON-RPC request IDs remain connection-local and need no global rewriting.
Server-initiated approval requests receive a synthetic fan-out ID, are persisted,
and are sent to eligible clients. The first response is forwarded to the
originating App Server connection; later responses are ignored idempotently.

### Session Registry

The registry maps `(adapter, project, external_session_key)` to a Codex thread
ID, working directory, and last activity. Updates use SQLite transactions so a
restart cannot produce two threads for one external session.

### Codex Adapter

Uses generated Codex App Server protocol shapes rather than terminal text. It
implements thread create/resume, turn start/steer, event streaming, interrupt,
and approval responses. It rejects unsupported Codex versions with a clear
compatibility error.

### cc-connect ACP Adapter

Runs over stdio as an ACP-compatible process spawned by cc-connect. It maps the
cc-connect session and working directory to the registry, translates prompts
and streamed output, and relays permission requests. Existing cc-connect
platform, attachment, `/list`, and `/switch` behavior remains owned by
cc-connect.

### Notification Policy

Every submitted turn carries an origin: `terminal`, `cc_connect`, or `system`.
A completed terminal/system turn may notify the active cc-connect chat. A
cc-connect-originated turn does not send a second completion notification because
the normal streamed/final reply already reaches that chat.

## Configuration And State

```text
~/.config/agent-notifier/
~/.local/state/agent-notifier/state.sqlite3
~/.local/state/agent-notifier/logs/
~/.local/share/agent-notifier/venv/
$XDG_RUNTIME_DIR/agent-notifier/*.sock
```

Configuration commands back up `~/.cc-connect/config.toml` and Codex hooks before
changing them, and record enough information for explicit restore commands.

## Failure Handling

- A dead App Server is restarted with bounded backoff; connected clients fail
  explicitly and can reconnect through cc-connect or a new TUI invocation.
- Requests in flight at process death fail explicitly; they are never silently
  replayed.
- Messages received during an active turn use `turn/steer` instead of starting a
  second competing turn.
- Approval resolution is idempotent and persisted before the response is sent.
- Unsupported external versions fail before service or adapter startup.

## Verification

- Unit tests cover registry transactions, origin policy, protocol routing, and
  approval first-writer-wins behavior.
- Integration tests use fake App Server and ACP clients over real streams.
- A fan-out test verifies that terminal and ACP responders share one persisted
  approval and only one response reaches Codex.
- Real smoke tests verify Codex 0.145.0 App Server initialization, cc-connect
  1.3.2 ACP session creation, and 60-second service stability. Live Feishu
  prompt/approval verification remains a deployment check.

## Explicit Non-Goals For V1

- OpenCode runtime support.
- Replacing the cc-connect platform layer.
- Parsing or reimplementing the Codex TUI.
- Synchronizing two independent Codex App Server processes.
- Remote network exposure; all sockets are local-user-only.
