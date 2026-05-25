#!/bin/bash
# setup_agent.sh — Create a new agent directory with all required files.
# ======================================================================
# This is the scripted version of AGENT_START_HERE.md steps 2-7.
# Run once per agent to set up their directory structure.
#
# Usage:
#   bash setup_agent.sh <agent_name> [agents_home]
#
# Examples:
#   bash setup_agent.sh Atlas                    # uses ~/agents
#   bash setup_agent.sh Rook /projects/agents    # custom path
#
# After running this, launch the agent with:
#   bash ops/launch_asdaaas.sh <agent_name>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ $# -lt 1 ]; then
    echo "Usage: bash setup_agent.sh <agent_name> [agents_home]"
    echo ""
    echo "Creates the agent directory structure with starter files."
    echo "agents_home defaults to ~/agents"
    exit 1
fi

AGENT_NAME="$1"
AGENTS_HOME="${2:-$HOME/agents}"
AGENT_HOME="$AGENTS_HOME/$AGENT_NAME"
ASDAAAS_DIR="${ASDAAAS_DIR:-$HOME/asdaaas}"

echo "=== Setting up agent: $AGENT_NAME ==="
echo "  Agent home: $AGENT_HOME"
echo "  Agents home: $AGENTS_HOME"
echo "  ASDAAAS dir: $ASDAAAS_DIR"
echo ""

# Check if agent already exists
if [ -d "$AGENT_HOME/asdaaas" ]; then
    echo "WARNING: Agent directory already exists at $AGENT_HOME"
    echo "  To reset, delete it first: rm -rf $AGENT_HOME"
    exit 1
fi

# Step 1: Create directory tree
echo "Creating directory structure..."
mkdir -p "$AGENT_HOME/asdaaas/"{doorbells,attention,profile,commands}
mkdir -p "$AGENT_HOME/asdaaas/adapters/"{irc,localmail,remind,session,context,heartbeat,tui}/{inbox,outbox}
mkdir -p "$ASDAAAS_DIR/adapters"

# Step 2: Create AGENTS.md
# Search for templates in multiple locations (flat layout and split layout)
SAMPLE=""
for candidate in \
    "$SCRIPT_DIR/../SAMPLE_AGENTS.md" \
    "$SCRIPT_DIR/../templates/SAMPLE_AGENTS.md" \
    "$SCRIPT_DIR/../../templates/SAMPLE_AGENTS.md" \
    "$SCRIPT_DIR/../../SAMPLE_AGENTS.md"; do
    if [ -f "$candidate" ]; then
        SAMPLE="$candidate"
        break
    fi
done

if [ -n "$SAMPLE" ]; then
    cp "$SAMPLE" "$AGENT_HOME/AGENTS.md"
    # Append agent instructions if available
    for instructions in "$SCRIPT_DIR/../ASDAAAS_AGENT_INSTRUCTIONS.md" "$SCRIPT_DIR/../../ASDAAAS_AGENT_INSTRUCTIONS.md"; do
        if [ -f "$instructions" ]; then
            echo "" >> "$AGENT_HOME/AGENTS.md"
            cat "$instructions" >> "$AGENT_HOME/AGENTS.md"
            break
        fi
    done
    echo "  Created AGENTS.md (from template + agent instructions)"
else
    echo "  WARNING: SAMPLE_AGENTS.md not found, skipping AGENTS.md"
fi

# Step 3: Create starter awareness and gaze (asdaaas auto-generates these too)
cat > "$AGENT_HOME/asdaaas/awareness.json" << 'EOF'
{
  "direct_attach": ["tui", "irc"],
  "control_watch": {},
  "notify_watch": [],
  "accept_from": ["*"],
  "default_doorbell": true,
  "doorbell_ttl": {"context": 1, "session": 2, "default": 3}
}
EOF
echo "  Created awareness.json"

cat > "$AGENT_HOME/asdaaas/gaze.json" << 'EOF'
{
  "speech": {"target": "tui", "params": {}},
  "thoughts": null
}
EOF
echo "  Created gaze.json"

# Step 4: Create lab notebook
cat > "$AGENT_HOME/lab_notebook.md" << EOF
# Lab Notebook — $AGENT_NAME

## $(date +"%Y-%m-%d %H:%M:%S %Z") — Agent initialized
About to do: Boot for the first time with agentabide infrastructure.
Why: Setting up continuous existence via asdaaas.
Expect: Receive first [continue] doorbell and begin operating.
EOF
echo "  Created lab_notebook.md"

# Step 5: Create notes to self
cat > "$AGENT_HOME/notes_to_self.md" << EOF
# Notes to Self — $AGENT_NAME

- Agent name: $AGENT_NAME
- Home: $AGENT_HOME
- Just initialized. Read AGENTS.md for full operating instructions.
- Infrastructure: asdaaas provides continuous existence via default doorbell.
- Read lab_notebook.md for history after compaction.
EOF
echo "  Created notes_to_self.md"

# Step 6: Register in running_agents.json
RUNNING="$ASDAAAS_DIR/running_agents.json"
if [ -f "$RUNNING" ]; then
    # Add to existing file
    python3 -c "
import json
with open('$RUNNING') as f:
    data = json.load(f)
if isinstance(data, list):
    data = {n: {} for n in data}
data['$AGENT_NAME'] = {'home': '$AGENT_HOME'}
with open('$RUNNING', 'w') as f:
    json.dump(data, f, indent=2)
"
    echo "  Added to running_agents.json"
else
    cat > "$RUNNING" << EOF
{
  "$AGENT_NAME": {"home": "$AGENT_HOME"}
}
EOF
    echo "  Created running_agents.json"
fi

echo ""
echo "=== Agent $AGENT_NAME is ready ==="
echo ""
echo "Next steps:"
echo "  1. Edit $AGENT_HOME/AGENTS.md — set agent name and role"
echo "  2. Authenticate grok: grok login --device-auth"
echo "  3. Launch: bash ops/launch_asdaaas.sh $AGENT_NAME"
echo "     (or: python3 core/asdaaas.py --agent $AGENT_NAME --cwd $AGENT_HOME)"
