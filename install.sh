#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
BIN_HOME=${XDG_BIN_HOME:-"$HOME/.local/bin"}
CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
VENV="$DATA_HOME/agent-notifier/venv"
BIN="$BIN_HOME/agent-notifier"
UNIT_HOME="$CONFIG_HOME/systemd/user"
NO_SERVICE=false

for arg in "$@"; do
  case "$arg" in
    --no-service) NO_SERVICE=true ;;
    uninstall) ;;
    *) echo "ERROR: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

uninstall() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now agent-notifier.service >/dev/null 2>&1 || true
  fi
  rm -f "$UNIT_HOME/agent-notifier.service" "$BIN"
  rm -rf "$VENV"
  rm -f "$CONFIG_HOME/opencode/plugins/feishu-notify.js"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
  echo "Removed agent-notifier program files."
  echo "Runtime state and cc-connect backups were intentionally preserved."
}

if [[ "${1:-}" == "uninstall" ]]; then
  uninstall
  exit 0
fi

command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required" >&2; exit 1; }
command -v codex >/dev/null 2>&1 || { echo "ERROR: codex is required" >&2; exit 1; }
CODEX_BINARY=$(command -v codex)
SERVICE_PATH=${PATH:-/usr/local/bin:/usr/bin:/bin}
ESCAPED_CODEX_BINARY=${CODEX_BINARY//&/\\&}
ESCAPED_SERVICE_PATH=${SERVICE_PATH//&/\\&}

mkdir -p "$DATA_HOME/agent-notifier" "$BIN_HOME"
if command -v uv >/dev/null 2>&1; then
  uv venv --clear --python python3 "$VENV"
  uv pip install --python "$VENV/bin/python" "$ROOT"
else
  rm -rf "$VENV"
  if ! python3 -m venv "$VENV"; then
    echo "ERROR: install uv or the OS python3-venv package" >&2
    exit 1
  fi
  "$VENV/bin/python" -m pip install --upgrade pip
  "$VENV/bin/python" -m pip install "$ROOT"
fi
ln -sfn "$VENV/bin/agent-notifier" "$BIN"
"$BIN" init-config
"$BIN" configure-native-hooks

if [[ "$NO_SERVICE" == false ]] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$UNIT_HOME"
  sed -e "s|@CODEX_BINARY@|$ESCAPED_CODEX_BINARY|g" \
    -e "s|@SERVICE_PATH@|$ESCAPED_SERVICE_PATH|g" \
    "$ROOT/systemd/agent-notifier.service" > "$UNIT_HOME/agent-notifier.service"
  systemctl --user daemon-reload
  systemctl --user enable agent-notifier.service
  systemctl --user restart agent-notifier.service
  SERVICE_READY=false
  for _ in $(seq 1 200); do
    if "$BIN" status 2>/dev/null | grep -q '^status: running$'; then
      SERVICE_READY=true
      break
    fi
    sleep 0.1
  done
  if [[ "$SERVICE_READY" == false ]]; then
    systemctl --user stop agent-notifier.service >/dev/null 2>&1 || true
    echo "ERROR: agent-notifier service did not become ready within 20 seconds" >&2
    echo "Inspect logs with: journalctl --user -u agent-notifier.service -n 100" >&2
    exit 1
  fi
  echo "Installed and restarted user service: agent-notifier.service"
elif [[ "$NO_SERVICE" == false ]]; then
  echo "systemd user service unavailable; agent-notifier will auto-start a background process."
else
  echo "Skipped service installation (--no-service)."
fi

echo "Installed: $BIN"

OPENCODE_PLUGIN_DIR="$CONFIG_HOME/opencode/plugins"
mkdir -p "$OPENCODE_PLUGIN_DIR"
cp "$ROOT/opencode-plugins/feishu-notify.js" "$OPENCODE_PLUGIN_DIR/"
if command -v opencode >/dev/null 2>&1; then
  echo "OpenCode plugin installed: $OPENCODE_PLUGIN_DIR/feishu-notify.js"
  WORK_DIR=$(pwd)
  OPENCODE_BIN=$(command -v opencode)
  if [[ "$NO_SERVICE" == false ]] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p "$UNIT_HOME"
    sed -e "s|@WORK_DIR@|$WORK_DIR|g" \
      -e "s|@OPENCODE_BIN@|$OPENCODE_BIN|g" \
      "$ROOT/systemd/opencode-serve.service" > "$UNIT_HOME/opencode-serve.service"
    systemctl --user daemon-reload
    systemctl --user enable opencode-serve.service
    systemctl --user restart opencode-serve.service
    echo "Opencode serve installed as user service: opencode-serve.service"
  else
    echo "Start opencode serve manually: opencode serve --port 4098"
  fi
else
  echo "OpenCode not found — plugin installed but requires OpenCode runtime"
  echo "OpenCode plugin: $OPENCODE_PLUGIN_DIR/feishu-notify.js"
  echo
  echo "After installing OpenCode, start the serve:"
  echo "  opencode serve --port 4098"
fi

"$BIN" doctor
echo
echo "If Codex already has Stop/PermissionRequest hooks: agent-notifier configure-hooks"
echo "Next: agent-notifier configure-cc --project <cc-connect-project-name>"
echo
echo "=== OpenCode approval card setup ==="
echo "Add these commands to ~/.cc-connect/config.toml:"
echo
echo "  [[commands]]"
echo "    name = \"opencode-approve\""
echo "    description = \"Approve an OpenCode permission request\""
echo "    exec = \"sh -c \\\"echo once > /tmp/oc-perm-reply-{{1}}.json && agent-notifier opencode-reply-result --perm {{1}} allow >/dev/null 2>&1 && date '+%Y-%m-%d %H:%M:%S'\\\"\""
echo
echo "  [[commands]]"
echo "    name = \"opencode-deny\""
echo "    description = \"Deny an OpenCode permission request\""
echo "    exec = \"sh -c \\\"echo reject > /tmp/oc-perm-reply-{{1}}.json && agent-notifier opencode-reply-result --perm {{1}} deny >/dev/null 2>&1 && date '+%Y-%m-%d %H:%M:%S'\\\"\""
echo
echo "Add this to ~/.config/agent-notifier/config.toml:"
echo
echo "  [chat_routes]"
echo "  oc_YOUR_GROUP_A_CHAT_ID = \"opencode:<session_id>\""
echo "  oc_YOUR_GROUP_B_CHAT_ID = \"silent\""
echo
echo "  [notification_routes.default]"
echo "  project = \"<cc-connect-project-name>\""
echo "  receive_id_type = \"chat_id\""
echo "  receive_id = \"oc_YOUR_GROUP_B_CHAT_ID\""
echo "  message_format = \"markdown\""
echo "  session_key = \"feishu:oc_YOUR_GROUP_B_CHAT_ID:terminal:<session_id>\""
echo
echo "  [notification_routes.acp]"
echo "  project = \"<cc-connect-project-name>\""
echo "  receive_id_type = \"chat_id\""
echo "  receive_id = \"oc_YOUR_GROUP_A_CHAT_ID\""
echo "  message_format = \"markdown\""
echo "  session_key = \"feishu:oc_YOUR_GROUP_A_CHAT_ID:terminal:<session_id>\""
echo
echo "Get chat IDs by sending /whoami in each group chat."
echo "Then register the terminal session with cc-connect:"
echo "  agent-notifier bind-terminal --thread-id <session_id> --route default"
echo "  agent-notifier bind-terminal --thread-id <session_id> --route acp"
