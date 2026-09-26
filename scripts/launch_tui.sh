#!/bin/bash
# Launch the asdaaas TUI for a specific agent.
#
# Usage:
#   bash launch_tui.sh --agent Trip
#   bash launch_tui.sh -a Sr --replay --tail 50
#   bash launch_tui.sh                    # interactive pick (TTY only; no default)
#   bash launch_tui.sh --operator eric    # pick agent, then pass other flags
#
# --agent / -a is optional when stdin is a TTY: you get a numbered citizen list.
# There is no silent default agent (not Wend, not anything).
# Non-interactive (no TTY) still requires --agent/-a.
#
# --api-url injected only if ASDAAAS_API_URL env var is set.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TUI="$SCRIPT_DIR/../tui/asdaaas_tui.py"
API_URL="${ASDAAAS_API_URL:-}"

usage() {
    echo "Usage: bash launch_tui.sh [--agent|-a NAME] [options]"
    echo ""
    echo "Options:"
    echo "  --agent, -a NAME    Agent name (optional on TTY → interactive pick; required otherwise)"
    echo "  --replay, -r        Replay session from beginning"
    echo "  --tail, -t N        Replay last N events only"
    echo "  --operator, -o NAME Operator name (skip prompt)"
    echo "  --sessions-dir DIR  Override sessions directory"
    echo "  --api-url URL       API server URL (default: \$ASDAAAS_API_URL, omitted if unset)"
    echo "  --debug-log PATH    Write dispatch debug log"
    echo "  --light             Use Grok Day light theme"
    echo "  --theme NAME        Theme id or auto (see tui/themes/)"
    echo ""
    echo "Examples:"
    echo "  bash launch_tui.sh --agent Trip"
    echo "  bash launch_tui.sh -a Sr --tail 30 -o eric"
    echo "  bash launch_tui.sh                 # pick from list"
}

# --- resolve agent from argv; if missing, optional interactive pick ---
has_agent=0
ARGS=()
prev=""
for arg in "$@"; do
    if [[ "$prev" == "--agent" || "$prev" == "-a" ]]; then
        has_agent=1
    fi
    if [[ "$arg" == "--agent" || "$arg" == "-a" ]]; then
        : # next token is name
    fi
    if [[ "$arg" == --agent=* ]]; then
        has_agent=1
    fi
    ARGS+=("$arg")
    prev="$arg"
done
# also detect -aName glued? uncommon; skip

if [[ $has_agent -eq 0 ]]; then
    # scan for -a NAME as two tokens already covered via prev
    # detect bare --agent without value at end → usage
    if [[ ! -t 0 ]]; then
        echo "Error: --agent/-a required when not a TTY (no default agent)." >&2
        usage
        exit 1
    fi
    CONFIG="${ASDAAAS_CONFIG:-}"
    if [[ -n "$CONFIG" && -f "$CONFIG/agents.json" ]]; then
        AGENTS_JSON="$CONFIG/agents.json"
    elif [[ -n "$CONFIG" && -f "$CONFIG" && "$CONFIG" == *.json ]]; then
        AGENTS_JSON="$CONFIG"
    elif [[ -f "${HOME}/agents/config/agents.json" ]]; then
        AGENTS_JSON="${HOME}/agents/config/agents.json"
    elif [[ -f /srv/config/agents.json ]]; then
        AGENTS_JSON=/srv/config/agents.json
    else
        AGENTS_JSON=""
    fi

    echo "No --agent given. Citizens (no default):"
    if [[ -n "$AGENTS_JSON" && -f "$AGENTS_JSON" ]]; then
        mapfile -t NAMES < <(python3 -c "import json; d=json.load(open(r'''$AGENTS_JSON''')); print('\n'.join(sorted((d.get('agents') or {}))))")
    else
        NAMES=(Jr Sr Cinco Trip Q Wend Squiggy)
    fi
    i=1
    for n in "${NAMES[@]}"; do
        printf "  %2d) %s\n" "$i" "$n"
        i=$((i + 1))
    done
    echo -n "Pick number or name (empty cancels): "
    read -r choice || true
    if [[ -z "${choice:-}" ]]; then
        echo "Cancelled — no agent selected."
        exit 1
    fi
    if [[ "$choice" =~ ^[0-9]+$ ]]; then
        idx=$((choice - 1))
        if [[ $idx -lt 0 || $idx -ge ${#NAMES[@]} ]]; then
            echo "Invalid number." >&2
            exit 1
        fi
        AGENT="${NAMES[$idx]}"
    else
        AGENT="$choice"
    fi
    # prepend --agent so python still sees required flag
    ARGS=(--agent "$AGENT" "${ARGS[@]}")
    echo "→ agent $AGENT"
fi

# Upgrade TERM to xterm-256color if available and not already set
if [[ "${TERM:-}" != *-256color ]] && command -v infocmp >/dev/null 2>&1 && infocmp xterm-256color &>/dev/null; then
    export TERM=xterm-256color
fi

if [ ! -f "$TUI" ]; then
    echo "Error: TUI not found at $TUI"
    exit 1
fi

# Inject --api-url only if ASDAAAS_API_URL env var is set
if [[ -n "$API_URL" ]] && [[ ! " ${ARGS[*]} " =~ " --api-url " ]]; then
    exec python3 "$TUI" --api-url "$API_URL" "${ARGS[@]}"
else
    exec python3 "$TUI" "${ARGS[@]}"
fi
