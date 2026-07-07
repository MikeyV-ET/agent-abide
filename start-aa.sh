#!/bin/bash
# start-aa.sh — One-command setup and launch for agent-abide.
#
# Usage:
#   bash start-aa.sh
#
# This script:
#   1. Checks prerequisites (Python, Node, grok, pip packages)
#   2. Asks what setup you want (3-agent team or single agent)
#   3. Creates agents.json and agent directories
#   4. Launches everything
#
# Safe to re-run — skips steps that are already done.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }

echo ""
echo "=== agent-abide setup ==="
echo ""

# ---- Step 1: Check prerequisites ----
echo "Checking prerequisites..."
MISSING=0

if command -v python3 &>/dev/null; then
    ok "Python $(python3 --version 2>&1 | awk '{print $2}')"
else
    fail "Python 3.11+ not found (install: https://python.org)"
    MISSING=1
fi

if command -v node &>/dev/null; then
    ok "Node $(node --version)"
else
    fail "Node.js 20+ not found (install: https://nodejs.org)"
    MISSING=1
fi

if command -v grok &>/dev/null; then
    ok "grok $(grok --version 2>/dev/null || echo '(version unknown)')"
else
    fail "grok CLI not found"
    echo "    Install: npm install -g @xai-official/grok"
    MISSING=1
fi

# Check pip packages
PKGS_OK=true
for pkg in requests websockets websocket-client textual rich; do
    if ! python3 -c "import $(echo $pkg | tr '-' '_')" 2>/dev/null; then
        PKGS_OK=false
        break
    fi
done
if [ "$PKGS_OK" = true ]; then
    ok "Python packages"
else
    warn "Missing Python packages — installing..."
    pip install -r "$SCRIPT_DIR/requirements.txt" || { fail "pip install failed"; MISSING=1; }
    ok "Python packages installed"
fi

# Check grok auth
if [ -f "$HOME/.grok/auth.json" ] || [ -n "${XAI_API_KEY:-}" ]; then
    ok "grok auth"
else
    warn "grok not authenticated"
    echo "    Run: grok login --device-auth"
    echo "    Or set XAI_API_KEY environment variable"
    MISSING=1
fi

if [ "$MISSING" -eq 1 ]; then
    echo ""
    fail "Prerequisites missing. Fix the issues above and re-run."
    exit 1
fi

echo ""

# ---- Step 2: Choose setup ----
if [ -f "$SCRIPT_DIR/agents.json" ]; then
    # agents.json exists — check if agents are configured
    AGENT_COUNT=$(python3 -c "import json; print(len(json.load(open('$SCRIPT_DIR/agents.json')).get('agents', {})))" 2>/dev/null || echo "0")
    if [ "$AGENT_COUNT" -gt 0 ]; then
        echo "Found existing agents.json with $AGENT_COUNT agent(s)."
        echo ""
        read -p "Launch existing setup? [Y/n] " LAUNCH_EXISTING
        if [ "${LAUNCH_EXISTING:-Y}" != "n" ] && [ "${LAUNCH_EXISTING:-Y}" != "N" ]; then
            echo ""
            echo "=== Launching ==="
            bash "$SCRIPT_DIR/scripts/launch_asdaaas.sh" --wait
            echo ""
            echo "=== Done ==="
            echo "TUI: bash scripts/launch_tui.sh"
            exit 0
        fi
    fi
fi

echo "How would you like to set up your agents?"
echo ""
echo "  1) Team of 3: Coder, Tester, Reviewer (recommended)"
echo "  2) Single agent"
echo "  3) Custom (you name them)"
echo ""
read -p "Choice [1/2/3]: " CHOICE

AGENTS_DIR="$HOME/agents"
read -p "Agent home directory [$AGENTS_DIR]: " CUSTOM_DIR
AGENTS_DIR="${CUSTOM_DIR:-$AGENTS_DIR}"

case "${CHOICE:-1}" in
    1)
        AGENT_NAMES=("Coder" "Tester" "Reviewer")
        AGENT_ROLES=("coder" "tester" "reviewer")
        ;;
    2)
        read -p "Agent name: " SINGLE_NAME
        AGENT_NAMES=("${SINGLE_NAME:-MyAgent}")
        AGENT_ROLES=("coder")
        ;;
    3)
        read -p "Agent names (comma-separated): " CUSTOM_NAMES
        IFS=',' read -ra AGENT_NAMES <<< "$CUSTOM_NAMES"
        AGENT_ROLES=()
        for _ in "${AGENT_NAMES[@]}"; do
            AGENT_ROLES+=("coder")
        done
        ;;
    *)
        echo "Invalid choice"
        exit 1
        ;;
esac

echo ""

# ---- Step 3: Create agents.json ----
echo "Creating agents.json..."

# Find grok binary path
GROK_BIN=$(command -v grok)

# Build agents JSON block
AGENTS_JSON="{"
for i in "${!AGENT_NAMES[@]}"; do
    name=$(echo "${AGENT_NAMES[$i]}" | xargs)  # trim whitespace
    [ $i -gt 0 ] && AGENTS_JSON+=","
    AGENTS_JSON+="\"$name\":{\"session\":\"\",\"home\":\"$AGENTS_DIR/$name\",\"observer_enabled\":true,\"interjection_enabled\":false}"
done
AGENTS_JSON+="}"

python3 -c "
import json
config = {
    'settings': {
        'asdaaas_dir': '$SCRIPT_DIR',
        'agents_dir': '$AGENTS_DIR',
        'log_dir': '/tmp',
        'running_agents_file': '$AGENTS_DIR/running_agents.json',
        'timezone': 'America/Los_Angeles',
        'debug': False,
        'grok_binary': '$GROK_BIN'
    },
    'agents': json.loads('$AGENTS_JSON'),
    'adapters': {}
}
with open('$SCRIPT_DIR/agents.json', 'w') as f:
    json.dump(config, f, indent=2)
print('  agents.json created')
"

# ---- Step 4: Create agent directories ----
echo "Creating agents..."
for i in "${!AGENT_NAMES[@]}"; do
    name=$(echo "${AGENT_NAMES[$i]}" | xargs)
    role="${AGENT_ROLES[$i]}"
    
    if [ -d "$AGENTS_DIR/$name/asdaaas" ]; then
        ok "$name (already exists)"
        continue
    fi
    
    bash "$SCRIPT_DIR/scripts/setup_agent.sh" "$name" "$AGENTS_DIR" 2>/dev/null
    
    # Apply role template if available
    if [ -f "$SCRIPT_DIR/templates/$role.md" ]; then
        # Insert role description into AGENTS.md
        ROLE_DESC=$(cat "$SCRIPT_DIR/templates/$role.md")
        python3 -c "
import re
with open('$AGENTS_DIR/$name/AGENTS.md') as f:
    content = f.read()
content = content.replace('AGENT_ROLE_DESCRIPTION', '''$ROLE_DESC'''.strip().split('\n', 2)[-1].strip())
with open('$AGENTS_DIR/$name/AGENTS.md', 'w') as f:
    f.write(content)
"
    fi
    
    # Replace ASDAAAS_DIR placeholder with actual path
    sed -i "s|<ASDAAAS_DIR>|$SCRIPT_DIR|g" "$AGENTS_DIR/$name/AGENTS.md"
    
    ok "$name ($role)"
done

# ---- Step 5: Launch ----
echo ""
echo "=== Launching ==="
bash "$SCRIPT_DIR/scripts/launch_asdaaas.sh" --wait "${AGENT_NAMES[@]}"

echo ""
echo "=== Setup complete ==="
echo ""
echo "Your agents are running. Next steps:"
echo "  TUI:        bash $SCRIPT_DIR/scripts/launch_tui.sh"
echo "  Check:      bash $SCRIPT_DIR/scripts/check_agent.sh ${AGENT_NAMES[0]}"
echo "  Stop:       bash $SCRIPT_DIR/scripts/stop_asdaaas.sh"
echo "  Smoke test: bash $SCRIPT_DIR/scripts/smoke_test.sh"
