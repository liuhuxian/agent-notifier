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

mkdir -p "$DATA_HOME/agent-notifier" "$BIN_HOME"
if command -v uv >/dev/null 2>&1; then
  uv venv --python python3 "$VENV"
  uv pip install --python "$VENV/bin/python" "$ROOT"
else
  if ! python3 -m venv "$VENV"; then
    echo "ERROR: install uv or the OS python3-venv package" >&2
    exit 1
  fi
  "$VENV/bin/python" -m pip install --upgrade pip
  "$VENV/bin/python" -m pip install "$ROOT"
fi
ln -sfn "$VENV/bin/agent-notifier" "$BIN"

if [[ "$NO_SERVICE" == false ]] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$UNIT_HOME"
  sed "s|@CODEX_BINARY@|$CODEX_BINARY|g" \
    "$ROOT/systemd/agent-notifier.service" > "$UNIT_HOME/agent-notifier.service"
  systemctl --user daemon-reload
  systemctl --user enable --now agent-notifier.service
  echo "Installed and started user service: agent-notifier.service"
elif [[ "$NO_SERVICE" == false ]]; then
  echo "systemd user service unavailable; agent-notifier will auto-start a background process."
else
  echo "Skipped service installation (--no-service)."
fi

echo "Installed: $BIN"
"$BIN" doctor
echo
echo "If Codex already has Stop/PermissionRequest hooks: agent-notifier configure-hooks"
echo "Next: agent-notifier configure-cc --project <cc-connect-project-name>"
