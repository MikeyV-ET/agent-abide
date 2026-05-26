#!/bin/bash
# Launch the asdaaas TUI for a specific agent.
#
# Usage:
#   bash launch_tui.sh --agent Trip
#   bash launch_tui.sh --agent Sr --replay --tail 50
#   bash launch_tui.sh --agent Jr --operator eric
#
# All arguments are passed through to asdaaas_tui.py.
# --agent is required.
# --api-url defaults to http://localhost:8420 (override with ASDAAAS_API_URL env var).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TUI="$SCRIPT_DIR/../tui/asdaaas_tui.py"
API_URL="${ASDAAAS_API_URL:-http://localhost:8420}"

if [ $# -eq 0 ] || [[ ! " $* " =~ " --agent " ]] && [[ ! " $* " =~ " -a " ]]; then
    echo "Usage: bash launch_tui.sh --agent <name> [options]"
    echo ""
    echo "Options:"
    echo "  --agent, -a NAME    Agent name (required)"
    echo "  --replay, -r        Replay session from beginning"
    echo "  --tail, -t N        Replay last N events only"
    echo "  --operator, -o NAME Operator name (skip prompt)"
    echo "  --sessions-dir DIR  Override sessions directory"
    echo "  --api-url URL       API server URL (default: \$ASDAAAS_API_URL or http://localhost:8420)"
    echo "  --debug-log PATH    Write dispatch debug log"
    echo "  --light             Use Gruvbox light color scheme"
    echo ""
    echo "Examples:"
    echo "  bash launch_tui.sh --agent Trip"
    echo "  bash launch_tui.sh -a Sr --tail 30 -o eric"
    exit 1
fi

if [ ! -f "$TUI" ]; then
    echo "Error: TUI not found at $TUI"
    exit 1
fi

# Inject --api-url unless the user already passed it
if [[ ! " $* " =~ " --api-url " ]]; then
    exec python3 "$TUI" --api-url "$API_URL" "$@"
else
    exec python3 "$TUI" "$@"
fi
