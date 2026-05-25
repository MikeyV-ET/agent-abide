#!/bin/bash
# Force-compact an agent's context. Bypasses confirmation step.
# Use when an agent is stuck at high context and can't self-compact.
#
# Usage:
#   bash force_compact.sh <AgentName>
#   bash force_compact.sh Q
#
# Reads agent home from agents.json. Writes force_compact command
# to the agent's command queue. asdaaas executes it on next loop iteration.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$SCRIPT_DIR/agents.json"

if [ ! -f "$CONFIG" ]; then
    echo "ERROR: Config file not found: $CONFIG"
    exit 1
fi

if [ $# -eq 0 ]; then
    echo "Usage: force_compact.sh <AgentName>"
    echo ""
    echo "Available agents:"
    python3 -c "import json; c=json.load(open('$CONFIG')); [print(f'  {n}') for n in c['agents']]"
    exit 1
fi

AGENT="$1"

# Validate agent exists
if ! python3 -c "import json,sys; c=json.load(open('$CONFIG')); sys.exit(0 if '$AGENT' in c['agents'] else 1)" 2>/dev/null; then
    echo "ERROR: Agent '$AGENT' not found in $CONFIG"
    exit 1
fi

# Get agent home
AGENT_HOME=$(python3 -c "import json; print(json.load(open('$CONFIG'))['agents']['$AGENT']['home'])")
CMD_DIR="$AGENT_HOME/asdaaas/commands"
mkdir -p "$CMD_DIR"

# Write force_compact command
CMD_FILE="$CMD_DIR/cmd_force_compact_$(date +%s).json"
echo '{"action": "force_compact"}' > "$CMD_FILE"

echo "Force compact command written for $AGENT"
echo "  asdaaas will execute on next loop iteration (within seconds)"
echo "  Check log: /tmp/asdaaas_$(echo "$AGENT" | tr '[:upper:]' '[:lower:]').log"
