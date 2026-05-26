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

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TUI="$SCRIPT_DIR/../tui/asdaaas_tui.py"

if [ $# -eq 0 ] || [[ ! " $* " =~ " --agent " ]] && [[ ! " $* " =~ " -a " ]]; then
    echo "Usage: bash launch_tui.sh --agent <name> [options]"
    echo ""
    echo "Options:"
    echo "  --agent, -a NAME    Agent name (required)"
    echo "  --replay, -r        Replay session from beginning"
    echo "  --tail, -t N        Replay last N events only"
    echo "  --operator, -o NAME Operator name (skip prompt)"
    echo "  --sessions-dir DIR  Override sessions directory"
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

exec python3 "$TUI" "$@"
