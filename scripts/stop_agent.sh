#!/bin/bash
# Stop a single asdaaas agent.
# Gracefully stops the agent process without relaunching.
#
# Usage:
#   bash stop_agent.sh <AgentName>           # stop one agent
#   bash stop_agent.sh <Agent1> <Agent2>     # stop multiple agents
#   bash stop_agent.sh --force <Agent>       # skip graceful shutdown
#   bash stop_agent.sh --list                # list configured agents
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
    echo "ERROR: Config file not found: $CONFIG"
    exit 1
fi

RUNNING_AGENTS_FILE=$(python3 -c "import json; c=json.load(open('$CONFIG')); print(c['settings']['running_agents_file'])")

TIMEOUT_GRACEFUL=30
TIMEOUT_TERM=10

# Parse args
FORCE=false
LIST=false
TARGETS=()
for arg in "$@"; do
    case "$arg" in
        --force) FORCE=true ;;
        --list)  LIST=true ;;
        *)       TARGETS+=("$arg") ;;
    esac
done

# List mode
if [ "$LIST" = true ]; then
    echo "Configured agents:"
    python3 -c "
import json
c = json.load(open('$CONFIG'))
for name, info in c['agents'].items():
    print(f'  {name}: session=...{info[\"session\"][-12:]}, home={info[\"home\"]}')
"
    exit 0
fi

if [ ${#TARGETS[@]} -eq 0 ]; then
    echo "Usage: stop_agent.sh <AgentName> [<AgentName2> ...]"
    echo "       stop_agent.sh --force <AgentName>"
    echo "       stop_agent.sh --list"
    exit 1
fi

# Validate all targets exist in config
for agent in "${TARGETS[@]}"; do
    if ! python3 -c "import json,sys; c=json.load(open('$CONFIG')); sys.exit(0 if '$agent' in c['agents'] else 1)" 2>/dev/null; then
        echo "ERROR: Agent '$agent' not found in $CONFIG"
        python3 -c "import json; c=json.load(open('$CONFIG')); [print(f'  {n}') for n in c['agents']]"
        exit 1
    fi
done

stop_agent() {
    local agent="$1"
    echo "Stopping $agent..."

    if ! pgrep -f "asdaaas.py --agent $agent" > /dev/null 2>&1; then
        echo "  $agent is not running"
        return 0
    fi

    if [ "$FORCE" = true ]; then
        pkill -KILL -f "asdaaas.py --agent $agent" 2>/dev/null
        echo "  $agent force-killed"
        sleep 1
        return 0
    fi

    # Graceful: write shutdown command
    local agent_home
    agent_home=$(python3 -c "import json; print(json.load(open('$CONFIG'))['agents']['$agent']['home'])")
    local cmd_dir="$agent_home/asdaaas/commands"
    mkdir -p "$cmd_dir"
    local cmd_file="$cmd_dir/cmd_shutdown_$(date +%s).json"
    echo '{"action": "shutdown"}' > "$cmd_file"
    echo "  Shutdown command written"

    # Wait for graceful exit
    local elapsed=0
    while [ $elapsed -lt $TIMEOUT_GRACEFUL ]; do
        if ! pgrep -f "asdaaas.py --agent $agent" > /dev/null 2>&1; then
            echo "  $agent stopped gracefully (${elapsed}s)"
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
        if [ $((elapsed % 5)) -eq 0 ]; then
            echo "  Waiting... (${elapsed}s)"
        fi
    done

    # SIGTERM
    echo "  $agent did not exit in ${TIMEOUT_GRACEFUL}s, sending SIGTERM..."
    pkill -TERM -f "asdaaas.py --agent $agent" 2>/dev/null
    sleep 2

    # SIGKILL if needed
    if pgrep -f "asdaaas.py --agent $agent" > /dev/null 2>&1; then
        local waited=0
        while [ $waited -lt $TIMEOUT_TERM ]; do
            if ! pgrep -f "asdaaas.py --agent $agent" > /dev/null 2>&1; then
                break
            fi
            sleep 1
            waited=$((waited + 1))
        done
        if pgrep -f "asdaaas.py --agent $agent" > /dev/null 2>&1; then
            echo "  Sending SIGKILL..."
            pkill -KILL -f "asdaaas.py --agent $agent" 2>/dev/null
            sleep 1
        fi
    fi
    echo "  $agent stopped"
}

for agent in "${TARGETS[@]}"; do
    stop_agent "$agent"

    # Remove from running_agents.json
    python3 -c "
import json, os
raf = '$RUNNING_AGENTS_FILE'
try:
    with open(raf) as f:
        ra = json.load(f)
    if '$agent' in ra:
        del ra['$agent']
        with open(raf + '.tmp', 'w') as f:
            json.dump(ra, f, indent=2)
        os.rename(raf + '.tmp', raf)
        print('  running_agents.json updated')
except (FileNotFoundError, json.JSONDecodeError):
    pass
"
done

echo "Done."