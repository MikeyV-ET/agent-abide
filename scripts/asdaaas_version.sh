#!/bin/bash
# asdaaas_version.sh -- Show which code version each agent is running
#
# Usage:
#   bash asdaaas_version.sh          # show all agents
#   bash asdaaas_version.sh Sr       # show one agent
#
# Reads health.json from each agent's asdaaas directory.
# Compare code_version against current HEAD to see if restart is needed.

AGENTS_HOME="${HOME}/agents"
INFRA_DIR="${HOME}/projects/mikeyv-infra"

current_head=$(git -C "$INFRA_DIR" rev-parse --short HEAD 2>/dev/null || echo "unknown")

printf "%-8s %-10s %-10s %-6s %s\n" "AGENT" "RUNNING" "HEAD" "MATCH" "LAST_ACTIVITY"
printf "%-8s %-10s %-10s %-6s %s\n" "-----" "-------" "----" "-----" "-------------"

check_agent() {
    local name="$1"
    local health="${AGENTS_HOME}/${name}/asdaaas/health.json"
    if [[ ! -f "$health" ]]; then
        printf "%-8s %-10s %-10s %-6s %s\n" "$name" "not found" "$current_head" "-" "-"
        return
    fi
    local version last_activity
    version=$(python3 -c "import json; d=json.load(open('$health')); print(d.get('code_version','pre-stamp'))" 2>/dev/null)
    last_activity=$(python3 -c "import json; d=json.load(open('$health')); print(d.get('last_activity','?'))" 2>/dev/null)

    local match="NO"
    if [[ "$version" == "$current_head" ]]; then
        match="YES"
    fi
    printf "%-8s %-10s %-10s %-6s %s\n" "$name" "$version" "$current_head" "$match" "$last_activity"
}

if [[ -n "$1" ]]; then
    check_agent "$1"
else
    for agent in Sr Jr Trip Q Cinco; do
        check_agent "$agent"
    done
fi