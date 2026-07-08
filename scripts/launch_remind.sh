#!/bin/bash
# Launch the remind adapter, detached from any session.
# Usage: bash scripts/launch_remind.sh
#
# Kills existing instance first, then starts fresh.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTERS="$SCRIPT_DIR/../adapters"

echo "=== Stopping existing remind adapter ==="
pkill -f "remind_adapter.py" 2>/dev/null && echo "Killed remind adapter" || echo "No remind adapter running"
sleep 1

echo ""
echo "=== Starting remind adapter ==="
setsid nohup python3 -u "$ADAPTERS/remind_adapter.py" > /tmp/remind_adapter.log 2>&1 &
echo "Remind adapter: $!"
ADAPTER_PID=$!

# Verify startup (2s grace period)
sleep 2
if kill -0 $ADAPTER_PID 2>/dev/null; then
    echo "=== Running (PID $ADAPTER_PID) ==="
else
    echo "=== FAILED — process exited immediately ==="
    tail -5 /tmp/remind_adapter.log 2>/dev/null
    exit 1
fi
echo "Log: /tmp/remind_adapter.log"
