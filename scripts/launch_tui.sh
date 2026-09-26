#!/bin/bash
# Launch the asdaaas TUI.
#
#   bash launch_tui.sh                 # blank TUI — pick agent in-app with [+]
#   bash launch_tui.sh -a Jr           # open Jr tab immediately
#   bash launch_tui.sh -a Jr -a Sr     # open multiple tabs
#
# --agent/-a is optional. No silent default agent.
# In-app [+] uses agents.json catalog (same as glass agent panel intent).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TUI="$SCRIPT_DIR/../tui/asdaaas_tui.py"
API_URL="${ASDAAAS_API_URL:-}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    echo "Usage: bash launch_tui.sh [--agent|-a NAME]… [options]"
    echo ""
    echo "  (no -a)             Blank TUI; pick citizen with tab-bar [+] after paint"
    echo "  --agent, -a NAME    Open this agent tab (repeatable)"
    echo "  --operator, -o NAME Operator name (skip prompt)"
    echo "  --replay / --tail   History catch-up options"
    echo "  --theme NAME | --light"
    exit 0
fi

if [[ "${TERM:-}" != *-256color ]] && command -v infocmp >/dev/null 2>&1 && infocmp xterm-256color &>/dev/null; then
    export TERM=xterm-256color
fi

if [[ ! -f "$TUI" ]]; then
    echo "Error: TUI not found at $TUI" >&2
    exit 1
fi

if [[ -n "$API_URL" ]] && [[ ! " $* " =~ " --api-url " ]]; then
    exec python3 "$TUI" --api-url "$API_URL" "$@"
else
    exec python3 "$TUI" "$@"
fi
