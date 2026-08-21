#!/bin/bash
# asdaaas_version.sh -- Show which code version each agent is running
#
# Usage:
#   bash asdaaas_version.sh          # show all agents from agents.json
#   bash asdaaas_version.sh Sr       # show one agent
#
# Reads health.json from each agent's resolved home (agents.json home field).

set -euo pipefail

if [ -n "${ASDAAAS_CONFIG:-}" ] && [ -d "$ASDAAAS_CONFIG" ]; then
    CONFIG="$ASDAAAS_CONFIG/agents.json"
elif [ -n "${ASDAAAS_CONFIG:-}" ] && [ -f "$ASDAAAS_CONFIG" ]; then
    CONFIG="$ASDAAAS_CONFIG"
else
    CONFIG="${HOME}/agents/config/agents.json"
fi

INFRA_DIR="${HOME}/projects/agent-abide"
current_head=$(git -C "$INFRA_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")

printf "%-12s %-10s %-10s %-6s %s\n" "AGENT" "RUNNING" "HEAD" "MATCH" "LAST_ACTIVITY"
printf "%-12s %-10s %-10s %-6s %s\n" "-----" "-------" "----" "-----" "-------------"

resolve_home() {
    local name="$1"
    if [ -f "$CONFIG" ]; then
        python3 -c "import json,sys; d=json.load(open(sys.argv[1])); a=d.get('agents',d).get(sys.argv[2],{}); print(a.get('home','') or '')" "$CONFIG" "$name" 2>/dev/null || true
    fi
}

check_agent() {
    local name="$1"
    local home
    home=$(resolve_home "$name")
    if [ -z "$home" ]; then
        home="${HOME}/agents/${name}"
    fi
    local health="${home}/asdaaas/health.json"
    if [[ ! -f "$health" ]]; then
        printf "%-12s %-10s %-10s %-6s %s\n" "$name" "not found" "$current_head" "-" "-"
        return
    fi
    local version last_activity
    version=$(python3 -c "import json; d=json.load(open('$health')); print(d.get('code_version','pre-stamp'))" 2>/dev/null)
    last_activity=$(python3 -c "import json; d=json.load(open('$health')); print(d.get('last_activity','?'))" 2>/dev/null)

    local match="NO"
    if [[ "$version" == "$current_head" ]]; then
        match="YES"
    fi
    printf "%-12s %-10s %-10s %-6s %s\n" "$name" "$version" "$current_head" "$match" "$last_activity"
}

if [[ -n "${1:-}" ]]; then
    check_agent "$1"
else
    if [ -f "$CONFIG" ]; then
        mapfile -t agents < <(python3 -c "import json; d=json.load(open('$CONFIG')); print('\\n'.join(d.get('agents',d).keys()))")
        for agent in "${agents[@]}"; do
            check_agent "$agent"
        done
    else
        for agent in Sr Jr Trip Q Cinco Squiggy; do
            check_agent "$agent"
        done
    fi
fi
