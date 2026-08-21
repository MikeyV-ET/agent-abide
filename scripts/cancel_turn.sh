#!/bin/bash
# Cancel an agent's current turn mid-flight.
#
# Creates a sentinel file that asdaaas watches during collect_response.
# When detected, asdaaas kills the grok process, restarts with session/load,
# and delivers a doorbell to the agent explaining what happened.
#
# Usage:
#   bash cancel_turn.sh Sr
#   bash cancel_turn.sh Q
#   bash cancel_turn.sh Squiggy

set -e

if [ -z "$1" ]; then
    echo "Usage: cancel_turn.sh <agent_name>"
    echo "Example: cancel_turn.sh Q"
    exit 1
fi

AGENT="$1"

# Resolve agent home from agents.json (nested homes OK)
if [ -n "${ASDAAAS_CONFIG:-}" ] && [ -d "$ASDAAAS_CONFIG" ]; then
    CONFIG="$ASDAAAS_CONFIG/agents.json"
elif [ -n "${ASDAAAS_CONFIG:-}" ] && [ -f "$ASDAAAS_CONFIG" ]; then
    CONFIG="$ASDAAAS_CONFIG"
else
    CONFIG="${HOME}/agents/config/agents.json"
fi

if [ -f "$CONFIG" ]; then
    AGENT_HOME=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); a=d.get('agents',d).get(sys.argv[2],{}); print(a.get('home','') or '')" "$CONFIG" "$AGENT" 2>/dev/null || true)
fi
if [ -z "${AGENT_HOME:-}" ]; then
    AGENT_HOME="$HOME/agents/$AGENT"
fi

FLAG_FILE="$AGENT_HOME/asdaaas/cancel_turn.flag"

if [ ! -d "$AGENT_HOME/asdaaas" ]; then
    echo "Error: Agent directory not found: $AGENT_HOME/asdaaas"
    exit 1
fi

touch "$FLAG_FILE"
echo "Cancel signal sent to $AGENT (flag: $FLAG_FILE)"
echo "asdaaas will kill the current turn, restart, and notify the agent."
