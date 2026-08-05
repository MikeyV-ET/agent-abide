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
# --api-url injected only if ASDAAAS_API_URL env var is set. Without it, TUI uses file-based tailing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TUI="$SCRIPT_DIR/../tui/asdaaas_tui.py"
API_URL="${ASDAAAS_API_URL:-}"

if [ $# -eq 0 ] || [[ ! " $* " =~ " --agent " ]] && [[ ! " $* " =~ " -a " ]]; then
    echo "Usage: bash launch_tui.sh --agent <name> [options]"
    echo ""
    echo "Options:"
    echo "  --agent, -a NAME    Agent name (required)"
    echo "  --replay, -r        Replay session from beginning"
    echo "  --tail, -t N        Replay last N events only"
    echo "  --operator, -o NAME Operator name (skip prompt)"
    echo "  --sessions-dir DIR  Override sessions directory"
    echo "  --api-url URL       API server URL (default: \$ASDAAAS_API_URL, omitted if unset)"
    echo "  --debug-log PATH    Write dispatch debug log"
    echo "  --light             Use Grok Day light theme
    echo "  --theme NAME        Theme id or auto (tui/themes/)""
    echo ""
    echo "Examples:"
    echo "  bash launch_tui.sh --agent Trip"
    echo "  bash launch_tui.sh -a Sr --tail 30 -o eric"
    exit 1
fi

# Upgrade TERM to xterm-256color if available and not already set
if [[ "$TERM" != *-256color ]] && infocmp xterm-256color &>/dev/null; then
    export TERM=xterm-256color
fi

if [ ! -f "$TUI" ]; then
    echo "Error: TUI not found at $TUI"
    exit 1
fi

# Inject --api-url only if ASDAAAS_API_URL env var is set
if [[ -n "$API_URL" ]] && [[ ! " $* " =~ " --api-url " ]]; then
    exec python3 "$TUI" --api-url "$API_URL" "$@"
else
    exec python3 "$TUI" "$@"
fi
