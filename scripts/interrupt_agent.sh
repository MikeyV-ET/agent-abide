#!/bin/bash
# Send an operator interrupt to an agent. Highest priority doorbell.
# Cancels any delay/sleep. Agent receives it on next turn.
#
# Usage:
#   bash interrupt_agent.sh <AgentName>                    # default message
#   bash interrupt_agent.sh <AgentName> "custom message"   # custom message
#   bash interrupt_agent.sh Q "stop what you're doing and compact"
#
# Reads agent home from agents.json.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/agents.json"

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: Config file not found: $CONFIG"
    exit 1
fi

if [ $# -eq 0 ]; then
    echo "Usage: interrupt_agent.sh <AgentName> [message]"
    echo ""
    echo "Available agents:"
    python3 -c "import json; c=json.load(open('$CONFIG')); [print(f'  {n}') for n in c['agents']]"
    exit 1
fi

AGENT="$1"
MESSAGE="${2:-Operator interrupt: please acknowledge and report status.}"

# Validate agent exists
if ! python3 -c "import json,sys; c=json.load(open('$CONFIG')); sys.exit(0 if '$AGENT' in c['agents'] else 1)" 2>/dev/null; then
    echo "ERROR: Agent '$AGENT' not found in $CONFIG"
    exit 1
fi

# Get agent home
AGENT_HOME=$(python3 -c "import json; print(json.load(open('$CONFIG'))['agents']['$AGENT']['home'])")
CMD_DIR="$AGENT_HOME/asdaaas/commands"
mkdir -p "$CMD_DIR"

# Write interrupt command
CMD_FILE="$CMD_DIR/cmd_interrupt_$(date +%s).json"
python3 -c "
import json
cmd = {'action': 'interrupt', 'text': '''$MESSAGE'''}
with open('$CMD_FILE', 'w') as f:
    json.dump(cmd, f)
"

echo "Interrupt sent to $AGENT"
echo "  Message: $MESSAGE"
echo "  Agent will receive it on next turn (delay cancelled)"
