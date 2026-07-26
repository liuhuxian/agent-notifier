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
BACKEND=auto

usage() {
  echo "Usage: ./install.sh [--backend auto|codex|opencode|multi] [--no-service]"
  echo "       ./install.sh uninstall"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-service) NO_SERVICE=true; shift ;;
    --backend) [[ $# -ge 2 ]] || { echo "ERROR: --backend needs a value" >&2; exit 2; }; BACKEND=$2; shift 2 ;;
    --backend=*) BACKEND=${1#--backend=}; shift ;;
    --help|-h) usage; exit 0 ;;
    uninstall) break ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

uninstall() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now agent-notifier.service >/dev/null 2>&1 || true
    systemctl --user disable --now opencode-serve.service >/dev/null 2>&1 || true
  fi
  rm -f "$UNIT_HOME/agent-notifier.service" "$UNIT_HOME/opencode-serve.service" "$BIN"
  rm -rf "$VENV"
  rm -f "$CONFIG_HOME/opencode/plugins/feishu-notify.js"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload >/dev/null 2>&1 || true
  fi
  echo "Removed agent-notifier program files and user services."
  echo "Runtime state, user config, and cc-connect backups were intentionally preserved."
}

if [[ "${1:-}" == "uninstall" ]]; then
  uninstall
  exit 0
fi

command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is required" >&2; exit 1; }
CODEX_BINARY=$(command -v codex || true)
OPENCODE_BINARY=$(command -v opencode || true)
if [[ "$BACKEND" == auto ]]; then
  if [[ -n "$CODEX_BINARY" && -n "$OPENCODE_BINARY" ]]; then BACKEND=multi
  elif [[ -n "$CODEX_BINARY" ]]; then BACKEND=codex
  elif [[ -n "$OPENCODE_BINARY" ]]; then BACKEND=opencode
  else echo "ERROR: install Codex or OpenCode first" >&2; exit 1; fi
fi
case "$BACKEND" in
  codex) [[ -n "$CODEX_BINARY" ]] || { echo "ERROR: Codex is required" >&2; exit 1; } ;;
  opencode) [[ -n "$OPENCODE_BINARY" ]] || { echo "ERROR: OpenCode is required" >&2; exit 1; } ;;
  multi) [[ -n "$CODEX_BINARY" && -n "$OPENCODE_BINARY" ]] || { echo "ERROR: Codex and OpenCode are required" >&2; exit 1; } ;;
  *) echo "ERROR: unsupported backend: $BACKEND" >&2; exit 2 ;;
esac

SERVICE_PATH=${PATH:-/usr/local/bin:/usr/bin:/bin}
ESCAPED_CODEX_BINARY=${CODEX_BINARY//&/\\&}
ESCAPED_SERVICE_PATH=${SERVICE_PATH//&/\\&}

mkdir -p "$DATA_HOME/agent-notifier" "$BIN_HOME"
if command -v uv >/dev/null 2>&1; then
  uv venv --clear --python python3 "$VENV"
  uv pip install --python "$VENV/bin/python" "$ROOT"
else
  rm -rf "$VENV"
  python3 -m venv "$VENV" || { echo "ERROR: install uv or python3-venv" >&2; exit 1; }
  "$VENV/bin/python" -m pip install --upgrade pip
  "$VENV/bin/python" -m pip install "$ROOT"
fi
ln -sfn "$VENV/bin/agent-notifier" "$BIN"
"$BIN" init-config
if [[ "$BACKEND" == codex || "$BACKEND" == multi ]]; then
  "$BIN" configure-native-hooks
fi

if [[ "$BACKEND" != opencode && "$NO_SERVICE" == false ]] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$UNIT_HOME"
  sed -e "s|@CODEX_BINARY@|$ESCAPED_CODEX_BINARY|g" -e "s|@SERVICE_PATH@|$ESCAPED_SERVICE_PATH|g" \
    "$ROOT/systemd/agent-notifier.service" > "$UNIT_HOME/agent-notifier.service"
  systemctl --user daemon-reload
  systemctl --user enable agent-notifier.service
  systemctl --user restart agent-notifier.service
  SERVICE_READY=false
  for _ in $(seq 1 200); do
    if "$BIN" status 2>/dev/null | grep -q '^status: running$'; then SERVICE_READY=true; break; fi
    sleep 0.1
  done
  if [[ "$SERVICE_READY" == false ]]; then
    systemctl --user stop agent-notifier.service >/dev/null 2>&1 || true
    echo "ERROR: agent-notifier service did not become ready; journalctl --user -u agent-notifier.service -n 100" >&2
    exit 1
  fi
  echo "Installed and restarted user service: agent-notifier.service"
elif [[ "$BACKEND" == opencode ]]; then
  echo "Codex service skipped for OpenCode-only installation."
elif [[ "$NO_SERVICE" == false ]]; then
  echo "systemd user service unavailable; agent-notifier will auto-start when needed."
else
  echo "Skipped service installation (--no-service)."
fi

echo "Installed: $BIN"
if [[ "$BACKEND" == opencode || "$BACKEND" == multi ]]; then
  OPENCODE_PLUGIN_DIR="$CONFIG_HOME/opencode/plugins"
  mkdir -p "$OPENCODE_PLUGIN_DIR"
  cp "$ROOT/opencode-plugins/feishu-notify.js" "$OPENCODE_PLUGIN_DIR/"
  WORK_DIR=${AGENT_NOTIFIER_OPENCODE_WORK_DIR:-"$HOME"}
  mkdir -p "$WORK_DIR"
  ESCAPED_WORK_DIR=${WORK_DIR//&/\\&}
  ESCAPED_OPENCODE_BINARY=${OPENCODE_BINARY//&/\\&}
  echo "OpenCode plugin installed: $OPENCODE_PLUGIN_DIR/feishu-notify.js"
  if [[ "$NO_SERVICE" == false ]] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    sed -e "s|@WORK_DIR@|$ESCAPED_WORK_DIR|g" -e "s|@OPENCODE_BIN@|$ESCAPED_OPENCODE_BINARY|g" \
      "$ROOT/systemd/opencode-serve.service" > "$UNIT_HOME/opencode-serve.service"
    systemctl --user daemon-reload
    systemctl --user enable opencode-serve.service
    systemctl --user restart opencode-serve.service
    echo "Installed and restarted user service: opencode-serve.service"
  else
    echo "Start OpenCode manually: $OPENCODE_BINARY serve --port 4098"
  fi
else
  echo "OpenCode integration skipped (backend=$BACKEND)."
fi

"$BIN" doctor
echo
echo "Next:   agent-notifier setup"
echo "Then:   agent-notifier configure-cc --project <cc-connect-project-name>"
echo "Verify: agent-notifier doctor --strict"
