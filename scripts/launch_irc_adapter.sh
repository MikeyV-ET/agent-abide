#!/bin/bash
# Launch the IRC adapter, detached from any session.
# Usage: bash scripts/launch_irc_adapter.sh
#
# Kills existing instance first, then starts fresh.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTERS="$SCRIPT_DIR/../adapters"

echo "=== Stopping existing IRC adapter ==="
pkill -f "irc_adapter.py" 2>/dev/null && echo "Killed IRC adapter" || echo "No IRC adapter running"
sleep 1

echo ""
echo "=== Starting IRC adapter ==="
setsid nohup python3 -u "$ADAPTERS/irc_adapter.py" > /tmp/irc_adapter.log 2>&1 &
echo "IRC adapter: $!"
ADAPTER_PID=$!

# Verify startup (2s grace period)
sleep 2
if kill -0 $ADAPTER_PID 2>/dev/null; then
    echo "=== Running (PID $ADAPTER_PID) ==="
else
    echo "=== FAILED — process exited immediately ==="
    tail -5 /tmp/irc_adapter.log 2>/dev/null
    exit 1
fi
echo "Log: /tmp/irc_adapter.log"
