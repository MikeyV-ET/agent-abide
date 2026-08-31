#!/bin/bash
# Launch all asdaaas agents + supporting adapters, detached from any session.
#
# Usage:
#   bash launch_asdaaas.sh              # launch all agents from agents.json
#   bash launch_asdaaas.sh Sr Trip      # launch specific agents only
#   bash launch_asdaaas.sh --wait Sr    # wait for agent to reach Ready before returning
#
# Reads configuration from agents.json (same directory as this script).
# Edit agents.json to add/remove agents, change paths, or modify settings.
#
# Kills existing instances first, then starts fresh.

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
    echo "Create agents.json with agent definitions. See AGENT_START_HERE.md."
    exit 1
fi

# Read settings from config
ASDAAAS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR=$(python3 -c "import json; c=json.load(open('$CONFIG')); print(c['settings']['log_dir'])")
RUNNING_AGENTS_FILE=$(python3 -c "import json; c=json.load(open('$CONFIG')); print(c['settings']['running_agents_file'])")
DEBUG=$(python3 -c "import json; c=json.load(open('$CONFIG')); print('1' if c['settings'].get('debug', False) else '')")
GROK_BINARY=$(python3 -c "import json; c=json.load(open('$CONFIG')); print(c['settings'].get('grok_binary', ''))")
ASDAAAS="$ASDAAAS_DIR/core/asdaaas.py"

# Parse --wait flag
WAIT_FOR_READY=false
ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--wait" ]; then
        WAIT_FOR_READY=true
    else
        ARGS+=("$arg")
    fi
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

# Determine which agents to launch
if [ $# -gt 0 ]; then
    AGENTS=("$@")
    # Validate
    for agent in "${AGENTS[@]}"; do
        if ! python3 -c "import json,sys; c=json.load(open('$CONFIG')); sys.exit(0 if '$agent' in c['agents'] else 1)" 2>/dev/null; then
            echo "ERROR: Agent '$agent' not found in $CONFIG"
            python3 -c "import json; c=json.load(open('$CONFIG')); [print(f'  {n}') for n in c['agents']]"
            exit 1
        fi
    done
    STOP_ALL=false
else
    AGENTS=($(python3 -c "import json; c=json.load(open('$CONFIG')); print(' '.join(c['agents'].keys()))"))
    STOP_ALL=true
fi

echo "=== Stopping existing instances ==="
if [ "$STOP_ALL" = true ]; then
    pkill -f "asdaaas.py --agent" 2>/dev/null && echo "Killed asdaaas agents" || echo "No asdaaas agents running"
    pkill -f "context_adapter.py" 2>/dev/null && echo "Killed context adapter" || echo "No context adapter running"
    pkill -f "session_adapter.py" 2>/dev/null && echo "Killed session adapter" || echo "No session adapter running"
    pkill -f "heartbeat_adapter.py" 2>/dev/null && echo "Killed heartbeat adapter" || echo "No heartbeat adapter running"
else
    for agent in "${AGENTS[@]}"; do
        pkill -f "asdaaas.py --agent $agent" 2>/dev/null && echo "Killed $agent" || echo "$agent not running"
    done
fi
sleep 2  # Wait for old shutdown handlers to finish (they unregister from running_agents.json)

# Write running_agents.json AFTER old processes are dead.
# Race condition: old shutdown handlers call _unregister_running_agent which
# removes entries from this file. If we write it before they finish, the old
# handler removes agents from the new file. (Hit this 2026-03-31 Session 39.)
python3 -c "
import json, os
config = json.load(open('$CONFIG'))
ra = {name: {'home': info['home']} for name, info in config['agents'].items()}
raf = '$RUNNING_AGENTS_FILE'
os.makedirs(os.path.dirname(raf), exist_ok=True)
with open(raf + '.tmp', 'w') as f:
    json.dump(ra, f, indent=2)
os.rename(raf + '.tmp', raf)
"
echo "running_agents.json updated"

if [ -n "$DEBUG" ]; then
    export ASDAAAS_DEBUG=1
fi

echo ""
echo "=== Starting asdaaas agents ==="

AGENT_NAMES_CSV=""
for agent in "${AGENTS[@]}"; do
    session=$(python3 -c "import json; print(json.load(open('$CONFIG'))['agents']['$agent']['session'])")
    home=$(python3 -c "import json; print(json.load(open('$CONFIG'))['agents']['$agent']['home'])")
    model=$(python3 -c "import json; print(json.load(open('$CONFIG'))['agents']['$agent'].get('model', ''))" 2>/dev/null)
    log_file="$LOG_DIR/asdaaas_$(echo "$agent" | tr '[:upper:]' '[:lower:]').log"

    MODEL_FLAG=""
    if [ -n "$model" ]; then
        MODEL_FLAG="--model $model"
    fi
    GROK_BIN_FLAG=""
    if [ -n "$GROK_BINARY" ]; then
        GROK_BIN_FLAG="--grok-binary $GROK_BINARY"
    fi
    # Prefer agents.json "user" so grok gets the citizen HOME (auth.json under ~/.grok).
    # Glass often launches via sudo root; without this HOME=/root and turns hang (no auth).
    agent_user=$(python3 -c "import json; print(json.load(open('$CONFIG'))['agents']['$agent'].get('user',''))" 2>/dev/null || true)
    run_cmd=(python3 -u "$ASDAAAS" --agent "$agent" --session "$session" --cwd "$home")
    if [ -n "$MODEL_FLAG" ]; then
        # shellcheck disable=SC2206
        run_cmd+=($MODEL_FLAG)
    fi
    if [ -n "$GROK_BIN_FLAG" ]; then
        # shellcheck disable=SC2206
        run_cmd+=($GROK_BIN_FLAG)
    fi
    if [ "$(id -u)" -eq 0 ] && [ -n "$agent_user" ] && id "$agent_user" >/dev/null 2>&1; then
        setsid nohup sudo -u "$agent_user" --preserve-env=ASDAAAS_CONFIG,ASDAAAS_DEBUG \
            env HOME="$home" USER="$agent_user" LOGNAME="$agent_user" \
            "${run_cmd[@]}" > "$log_file" 2>&1 &
        echo "$agent: PID $! (user=$agent_user home=$home session=$session model=${model:-default} log=$log_file)"
    else
        setsid nohup env HOME="$home" "${run_cmd[@]}" > "$log_file" 2>&1 &
        echo "$agent: PID $! (session=$session, model=${model:-default}, log=$log_file)"
    fi

    if [ -n "$AGENT_NAMES_CSV" ]; then
        AGENT_NAMES_CSV="$AGENT_NAMES_CSV,$agent"
    else
        AGENT_NAMES_CSV="$agent"
    fi
done

# --wait: until "[asdaaas] Ready." (90s timeout).
# IMPORTANT: do not use `tail -f | while read; exit 0` — exit only kills the
# while subshell; tail -f keeps running until timeout (~90s) even after Ready.
# grep -m1 closes the pipe so tail gets SIGPIPE and the wait ends promptly.
if [ "$WAIT_FOR_READY" = true ]; then
    echo ""
    for agent in "${AGENTS[@]}"; do
        log_file="$LOG_DIR/asdaaas_$(echo "$agent" | tr '[:upper:]' '[:lower:]').log"
        echo "Waiting for $agent..."
        # Ensure log exists so tail doesn't race-exit
        : >> "$log_file" 2>/dev/null || true
        if timeout 90 bash -c 'tail -n +1 -f "$1" 2>/dev/null | grep -m1 --line-buffered "\[asdaaas\] Ready\."' bash "$log_file" >/dev/null; then
            echo "$agent: Ready"
        else
            # Ready may already be in the file if we raced past it
            if grep -q '\[asdaaas\] Ready\.' "$log_file" 2>/dev/null; then
                echo "$agent: Ready"
            else
                echo "$agent: Timed out after 90s (check $log_file)"
            fi
        fi
    done
fi

if [ "$STOP_ALL" = true ]; then
    echo ""
    echo "=== Starting adapters ==="

    # Context adapter -- token threshold doorbells (45/65/80/88%)
    setsid nohup python3 -u "$ASDAAAS_DIR/adapters/context_adapter.py" --agents "$AGENT_NAMES_CSV" > "$LOG_DIR/context_adapter.log" 2>&1 &
    echo "Context adapter: PID $!"

    # Session adapter -- compact/status commands
    setsid nohup python3 -u "$ASDAAAS_DIR/adapters/session_adapter.py" --agents "$AGENT_NAMES_CSV" > "$LOG_DIR/session_adapter.log" 2>&1 &
    echo "Session adapter: PID $!"

    # Heartbeat adapter -- idle nudges
    setsid nohup python3 -u "$ASDAAAS_DIR/adapters/heartbeat_adapter.py" --agents "$AGENT_NAMES_CSV" > "$LOG_DIR/heartbeat_adapter.log" 2>&1 &
    echo "Heartbeat adapter: PID $!"
fi

echo ""
echo "=== All started ==="
echo "Agents: ${AGENTS[*]}"
echo "Logs: $LOG_DIR/asdaaas_*.log"
