#!/bin/bash
# Gracefully stop asdaaas agents and adapters.
#
# Usage:
#   bash stop_asdaaas.sh           # stop all agents + adapters
#   bash stop_asdaaas.sh Sr Trip   # stop specific agents only
#   bash stop_asdaaas.sh --force   # skip graceful, just kill
#
# Reads configuration from agents.json (same directory as this script).
# Edit agents.json to add/remove agents, change paths, or modify settings.
#
# Graceful shutdown:
#   1. Writes {"action": "shutdown"} to each agent's commands directory
#   2. Waits up to 30s for processes to exit (agents finish current turn)
#   3. If still running, sends SIGTERM (which also triggers graceful path)
#   4. If STILL running after 10 more seconds, SIGKILL
#
# Adapters get SIGTERM directly (they're stateless).

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
    echo "Create agents.json with agent definitions. See AGENT_START_HERE.md."
    exit 1
fi

TIMEOUT_GRACEFUL=30
TIMEOUT_TERM=10

# Parse args
FORCE=false
TARGETS=()
for arg in "$@"; do
    if [ "$arg" = "--force" ]; then
        FORCE=true
    else
        TARGETS+=("$arg")
    fi
done

# Default: all agents from agents.json
if [ ${#TARGETS[@]} -eq 0 ]; then
    TARGETS=($(python3 -c "import json; c=json.load(open('$CONFIG')); print(' '.join(c['agents'].keys()))"))
    STOP_ADAPTERS=true
else
    # Validate targets
    for agent in "${TARGETS[@]}"; do
        if ! python3 -c "import json,sys; c=json.load(open('$CONFIG')); sys.exit(0 if '$agent' in c['agents'] else 1)" 2>/dev/null; then
            echo "ERROR: Agent '$agent' not found in $CONFIG"
            python3 -c "import json; c=json.load(open('$CONFIG')); [print(f'  {n}') for n in c['agents']]"
            exit 1
        fi
    done
    STOP_ADAPTERS=false
fi

if [ ${#TARGETS[@]} -eq 0 ]; then
    echo "No agents found in agents.json"
    exit 0
fi

echo "=== Stopping agents: ${TARGETS[*]} ==="

if [ "$FORCE" = true ]; then
    echo "Force mode: skipping graceful shutdown"
    for agent in "${TARGETS[@]}"; do
        pkill -f "asdaaas.py --agent $agent" 2>/dev/null && echo "  Killed $agent" || echo "  $agent not running"
    done
else
    # Step 1: Write shutdown commands
    echo "Step 1: Sending shutdown commands..."
    for agent in "${TARGETS[@]}"; do
        agent_home=$(python3 -c "import json; print(json.load(open('$CONFIG'))['agents']['$agent']['home'])")
        cmd_dir="$agent_home/asdaaas/commands"
        mkdir -p "$cmd_dir"
        echo '{"action": "shutdown"}' > "$cmd_dir/cmd_shutdown_$(date +%s).json"
        echo "  $agent: shutdown command written"
    done

    # Step 2: Wait for graceful exit
    echo "Step 2: Waiting up to ${TIMEOUT_GRACEFUL}s for graceful exit..."
    elapsed=0
    while [ $elapsed -lt $TIMEOUT_GRACEFUL ]; do
        all_stopped=true
        for agent in "${TARGETS[@]}"; do
            if pgrep -f "asdaaas.py --agent $agent" > /dev/null 2>&1; then
                all_stopped=false
                break
            fi
        done
        if [ "$all_stopped" = true ]; then
            echo "  All agents stopped gracefully"
            break
        fi
        sleep 1
        elapsed=$((elapsed + 1))
        # Progress every 5s
        if [ $((elapsed % 5)) -eq 0 ]; then
            still_running=()
            for agent in "${TARGETS[@]}"; do
                if pgrep -f "asdaaas.py --agent $agent" > /dev/null 2>&1; then
                    still_running+=("$agent")
                fi
            done
            echo "  ${elapsed}s: still running: ${still_running[*]}"
        fi
    done

    # Step 3: SIGTERM for stragglers
    for agent in "${TARGETS[@]}"; do
        if pgrep -f "asdaaas.py --agent $agent" > /dev/null 2>&1; then
            echo "  $agent still running after ${TIMEOUT_GRACEFUL}s, sending SIGTERM..."
            pkill -TERM -f "asdaaas.py --agent $agent" 2>/dev/null
        fi
    done

    # Step 4: Wait for SIGTERM, then SIGKILL
    sleep 2
    for agent in "${TARGETS[@]}"; do
        if pgrep -f "asdaaas.py --agent $agent" > /dev/null 2>&1; then
            echo "  Waiting ${TIMEOUT_TERM}s for $agent SIGTERM..."
            waited=0
            while [ $waited -lt $TIMEOUT_TERM ]; do
                if ! pgrep -f "asdaaas.py --agent $agent" > /dev/null 2>&1; then
                    break
                fi
                sleep 1
                waited=$((waited + 1))
            done
            if pgrep -f "asdaaas.py --agent $agent" > /dev/null 2>&1; then
                echo "  $agent did not exit, sending SIGKILL"
                pkill -KILL -f "asdaaas.py --agent $agent" 2>/dev/null
            fi
        fi
    done
fi

# Stop adapters (only when stopping all agents)
if [ "$STOP_ADAPTERS" = true ]; then
    echo ""
    echo "=== Stopping adapters ==="
    # Read adapter scripts from agents.json instead of hardcoding
    for adapter in $(python3 -c "import json; c=json.load(open('$CONFIG')); print(' '.join(a['script'] for a in c.get('adapters',{}).values()))"); do
        pkill -TERM -f "$adapter" 2>/dev/null && echo "  Stopped $adapter" || echo "  $adapter not running"
    done
fi

echo ""
echo "=== Done ==="

# Show any remaining processes and offer to kill them
remaining=$(pgrep -f "asdaaas.py --agent" 2>/dev/null | wc -l)
if [ "$remaining" -gt 0 ]; then
    echo "WARNING: $remaining asdaaas process(es) still running"
    pgrep -af "asdaaas.py --agent" 2>/dev/null
    echo ""
    echo "These may be agents not listed in agents.json."
    echo "To force-kill all asdaaas processes: pkill -f 'asdaaas.py --agent'"
else
    echo "All asdaaas processes stopped"
fi