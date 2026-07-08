#!/bin/bash
# Check the health status of running asdaaas agents.
#
# Usage:
#   bash check_agent.sh              # check all configured agents
#   bash check_agent.sh Sr Trip      # check specific agents
#   bash check_agent.sh --verbose    # show log tail for each agent
#
# Reads configuration from agents.json (same directory as this script).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Config resolution: ASDAAAS_CONFIG env var (dir or file), then repo root
if [ -n "${ASDAAAS_CONFIG:-}" ] && [ -d "$ASDAAAS_CONFIG" ]; then
    CONFIG="$ASDAAAS_CONFIG/agents.json"
elif [ -n "${ASDAAAS_CONFIG:-}" ] && [ -f "$ASDAAAS_CONFIG" ]; then
    CONFIG="$ASDAAAS_CONFIG"
else
    CONFIG="$SCRIPT_DIR/../agents.json"
fi

if [ ! -f "$CONFIG" ]; then
    echo "FAIL: Config file not found: $CONFIG"
    exit 1
fi

LOG_DIR=$(python3 -c "import json; c=json.load(open('$CONFIG')); print(c['settings']['log_dir'])")

# Parse args
VERBOSE=false
TARGETS=()
for arg in "$@"; do
    case "$arg" in
        --verbose|-v) VERBOSE=true ;;
        *)            TARGETS+=("$arg") ;;
    esac
done

# Default: all agents
if [ ${#TARGETS[@]} -eq 0 ]; then
    TARGETS=($(python3 -c "import json; c=json.load(open('$CONFIG')); print(' '.join(c['agents'].keys()))"))
fi

UP=0
DOWN=0
WARN=0

for agent in "${TARGETS[@]}"; do
    home=$(python3 -c "import json; print(json.load(open('$CONFIG'))['agents']['$agent']['home'])" 2>/dev/null || true)
    if [ -z "$home" ]; then
        echo "  $agent  UNKNOWN  (not in agents.json)"
        DOWN=$((DOWN + 1))
        continue
    fi

    log_file="$LOG_DIR/asdaaas_$(echo "$agent" | tr '[:upper:]' '[:lower:]').log"
    health_file="$home/asdaaas/health.json"

    # Check process
    if pgrep -f "asdaaas.py --agent $agent" > /dev/null 2>&1; then
        pid=$(pgrep -f "asdaaas.py --agent $agent" | head -1)
        proc_status="running (PID $pid)"
    else
        proc_status="NOT RUNNING"
    fi

    # Check health file
    if [ -f "$health_file" ]; then
        health_info=$(python3 -c "
import json, time
h = json.load(open('$health_file'))
status = h.get('status', '?')
tokens = h.get('totalTokens', 0)
ctx = h.get('contextWindow', 0)
pct = round(tokens / ctx * 100, 1) if ctx > 0 else 0
ts = h.get('ts', h.get('timestamp', ''))
# Check staleness
try:
    from datetime import datetime
    dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    age = (datetime.now(dt.tzinfo) - dt).total_seconds()
    if age > 3600:
        age_str = f'{int(age/3600)}h ago'
    elif age > 60:
        age_str = f'{int(age/60)}m ago'
    else:
        age_str = f'{int(age)}s ago'
except:
    age_str = '?'
    age = 0
stale = ' STALE' if age > 300 else ''
print(f'status={status}, {pct}% context, updated {age_str}{stale}')
" 2>/dev/null || echo "error reading health")
    else
        health_info="no health file"
    fi

    # Check for stale commands in queue
    cmd_dir="$home/asdaaas/commands"
    stale_cmds=0
    if [ -d "$cmd_dir" ]; then
        for f in "$cmd_dir"/cmd_*.json; do
            [ -f "$f" ] && stale_cmds=$((stale_cmds + 1))
        done
    fi
    cmd_info=""
    if [ $stale_cmds -gt 0 ]; then
        cmd_info=" ($stale_cmds pending cmd)"
    fi

    # Check doorbells
    bell_dir="$home/asdaaas/doorbells"
    bell_count=0
    if [ -d "$bell_dir" ]; then
        bell_count=$(ls "$bell_dir"/*.json 2>/dev/null | wc -l || true)
    fi
    bell_info=""
    if [ "$bell_count" -gt 0 ] 2>/dev/null; then
        bell_info=" ($bell_count doorbell)"
    fi

    # Determine overall status
    if echo "$proc_status" | grep -q "NOT RUNNING"; then
        symbol="DOWN"
        DOWN=$((DOWN + 1))
    elif echo "$health_info" | grep -q "STALE"; then
        symbol="WARN"
        WARN=$((WARN + 1))
    else
        symbol="UP  "
        UP=$((UP + 1))
    fi

    printf "  %-6s %-4s  proc=%s  health=%s%s%s\n" "$agent" "$symbol" "$proc_status" "$health_info" "$cmd_info" "$bell_info"

    if [ "$VERBOSE" = true ] && [ -f "$log_file" ]; then
        echo "    --- Last 5 log lines ---"
        tail -5 "$log_file" 2>/dev/null | sed 's/^/    | /'
        echo "    ---"
    fi
done

echo ""
echo "Summary: $UP up, $DOWN down, $WARN warning"
if [ $DOWN -gt 0 ]; then
    exit 1
fi
