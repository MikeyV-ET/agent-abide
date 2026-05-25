#!/bin/bash
# Cancel an agent's current turn mid-flight.
#
# Creates a sentinel file that asdaaas watches during collect_response.
# When detected, asdaaas kills the grok process, restarts with session/load,
# and delivers a doorbell to the agent explaining what happened.
#
# The partial turn is lost but session state is preserved.
#
# Usage:
#   bash cancel_turn.sh Sr
#   bash cancel_turn.sh Q
#   bash cancel_turn.sh Trip

set -e

if [ -z "$1" ]; then
    echo "Usage: cancel_turn.sh <agent_name>"
    echo "Example: cancel_turn.sh Q"
    exit 1
fi

AGENT="$1"
FLAG_FILE="$HOME/agents/$AGENT/asdaaas/cancel_turn.flag"

# Check agent directory exists
if [ ! -d "$HOME/agents/$AGENT/asdaaas" ]; then
    echo "Error: Agent directory not found: $HOME/agents/$AGENT/asdaaas"
    exit 1
fi

# Create the flag file
touch "$FLAG_FILE"
echo "Cancel signal sent to $AGENT (flag: $FLAG_FILE)"
echo "asdaaas will kill the current turn, restart, and notify the agent."
