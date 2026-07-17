#!/bin/bash
# Compaction hook — writes compaction phase to per-agent binary_state.json
# Fired by grok binary on PreCompact and PostCompact events.
# Receives JSON envelope on stdin with hookEventName, sessionId, source, cwd, etc.

set -euo pipefail

ENVELOPE=$(cat)
EVENT=$(echo "$ENVELOPE" | python3 -c "import sys,json; print(json.load(sys.stdin)['hookEventName'])")
SESSION_ID=$(echo "$ENVELOPE" | python3 -c "import sys,json; print(json.load(sys.stdin)['sessionId'])")
CWD=$(echo "$ENVELOPE" | python3 -c "import sys,json; print(json.load(sys.stdin)['cwd'])")
SOURCE=$(echo "$ENVELOPE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('source','unknown'))")
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Derive agent name from CWD (e.g. /home/eric/agents/Sr -> Sr)
AGENT_NAME=$(basename "$CWD")
AGENTS_DIR="$HOME/agents"
STATE_FILE="$AGENTS_DIR/$AGENT_NAME/asdaaas/binary_state.json"

# Only act if we recognize the agent
if [ ! -d "$AGENTS_DIR/$AGENT_NAME/asdaaas" ]; then
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
with open(path, 'w') as f:
    json.dump(state, f, indent=2)
"
        ;;
esac
