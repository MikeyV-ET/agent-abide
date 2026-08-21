#!/bin/bash
# Compaction hook — writes compaction phase to per-agent binary_state.json
# Fired by grok binary on PreCompact and PostCompact events.
# Receives JSON envelope on stdin with hookEventName, sessionId, source, cwd, etc.
#
# Path rule: agent_home/asdaaas/binary_state.json
#   1. AGENT_HOME env (set by asdaaas when spawning binary)
#   2. envelope cwd (asdaaas starts binary with cwd = agent home)
# Never rebuild $HOME/agents/$(basename cwd) — that breaks nested homes.

set -euo pipefail

ENVELOPE=$(cat)
EVENT=$(echo "$ENVELOPE" | python3 -c "import sys,json; print(json.load(sys.stdin)['hookEventName'])")
SESSION_ID=$(echo "$ENVELOPE" | python3 -c "import sys,json; print(json.load(sys.stdin)['sessionId'])")
CWD=$(echo "$ENVELOPE" | python3 -c "import sys,json; print(json.load(sys.stdin)['cwd'])")
SOURCE=$(echo "$ENVELOPE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('source','unknown'))")
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

if [ -n "${AGENT_HOME:-}" ]; then
    AGENT_ROOT="$AGENT_HOME"
elif [ -n "${CWD:-}" ]; then
    AGENT_ROOT="$CWD"
else
    exit 0
fi

STATE_FILE="$AGENT_ROOT/asdaaas/binary_state.json"

# Only act if we recognize the agent tree
if [ ! -d "$AGENT_ROOT/asdaaas" ]; then
    exit 0
fi

case "$EVENT" in
    pre_compact)
        python3 -c "
import json, os
path = '$STATE_FILE'
state = {}
if os.path.exists(path):
    with open(path) as f:
        state = json.load(f)
state['compaction'] = {
    'phase': 'in_flight',
    'source': '$SOURCE',
    'session_id': '$SESSION_ID',
    'started_at': '$TIMESTAMP'
}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, 'w') as f:
    json.dump(state, f, indent=2)
"
        ;;
    post_compact)
        python3 -c "
import json, os
path = '$STATE_FILE'
state = {}
if os.path.exists(path):
    with open(path) as f:
        state = json.load(f)
state['compaction'] = {
    'phase': 'complete',
    'source': '$SOURCE',
    'session_id': '$SESSION_ID',
    'completed_at': '$TIMESTAMP'
}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, 'w') as f:
    json.dump(state, f, indent=2)
"
        ;;
esac
