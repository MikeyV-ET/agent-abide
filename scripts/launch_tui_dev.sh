#!/usr/bin/env bash
# aa-dev TUI (hot + paint_fold). Not prod agent-abide.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="${ROOT}/tui:${ROOT}/core:${PYTHONPATH:-}"
exec python3 -u tui/asdaaas_tui.py "$@"
