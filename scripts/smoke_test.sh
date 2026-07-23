#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$ROOT/src"

echo "[1/3] Running unit tests"
python3 -m unittest discover -s "$ROOT/tests" -v

echo "[2/3] Checking supported local versions"
python3 -m agent_notifier.cli doctor

echo "[3/3] Checking CLI surface"
python3 -m agent_notifier.cli --help >/dev/null
python3 -m agent_notifier.cli configure-cc --help >/dev/null

echo "Smoke test passed"
