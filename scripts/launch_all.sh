#!/bin/bash
# Launch the entire asdaaas stack: IRC server, adapters, agents.
#
# Usage:
#   bash launch_all.sh          # start everything
#   bash launch_all.sh --skip-irc-server  # skip miniircd (already running)
#
# This calls the individual launch scripts in dependency order:
#   1. IRC server (miniircd) -- needed by IRC adapter
#   2. asdaaas agents + context/session/heartbeat adapters
#   3. IRC adapter -- needs IRC server + agents
#   4. Localmail adapter -- needs agent directories
#   5. Remind adapter -- needs agent directories
#
# Each sub-script kills its own existing instances before starting.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SKIP_IRC_SERVER=false
for arg in "$@"; do
    case "$arg" in
        --skip-irc-server) SKIP_IRC_SERVER=true ;;
    esac
done

echo "============================================"
echo "  ASDAAAS Full Stack Launch"
echo "============================================"
echo ""

# 1. IRC server
if [ "$SKIP_IRC_SERVER" = false ]; then
    echo "--- Step 1/5: IRC server ---"
    bash "$SCRIPT_DIR/launch_irc_server.sh"
    sleep 1
    echo ""
else
    echo "--- Step 1/5: IRC server (skipped) ---"
    echo ""
fi

# 2. asdaaas agents + built-in adapters (context, session, heartbeat)
echo "--- Step 2/5: asdaaas agents + core adapters ---"
bash "$SCRIPT_DIR/launch_asdaaas.sh"
sleep 2
echo ""

# 3. IRC adapter
echo "--- Step 3/5: IRC adapter ---"
bash "$SCRIPT_DIR/launch_irc_adapter.sh"
echo ""

# 4. Localmail adapter
echo "--- Step 4/5: Localmail adapter ---"
bash "$SCRIPT_DIR/launch_localmail.sh"
echo ""

# 5. Remind adapter
echo "--- Step 5/5: Remind adapter ---"
bash "$SCRIPT_DIR/launch_remind.sh"
echo ""

echo "============================================"
echo "  All services started"
echo "============================================"
echo ""
echo "Logs:"
echo "  IRC server:    /tmp/miniircd.log"
echo "  Agents:        /tmp/asdaaas_*.log"
echo "  IRC adapter:   /tmp/irc_adapter.log"
echo "  Localmail:     /tmp/localmail_adapter.log"
echo "  Remind:        /tmp/remind_adapter.log"
echo ""
echo "Status: python3 $SCRIPT_DIR/ops_dashboard.py --once"
