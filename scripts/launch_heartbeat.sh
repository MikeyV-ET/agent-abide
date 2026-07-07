#!/bin/bash
# Launch the heartbeat adapter, detached from any session.
# Usage: bash scripts/launch_heartbeat.sh
#
# Kills existing instance first, then starts fresh.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTERS="$SCRIPT_DIR/../adapters"

echo "=== Stopping existing heartbeat adapter ==="
pkill -f "heartbeat_adapter.py" 2>/dev/null && echo "Killed heartbeat adapter" || echo "No heartbeat adapter running"
sleep 1

echo ""
echo "=== Starting heartbeat adapter ==="
setsid nohup python3 -u "$ADAPTERS/heartbeat_adapter.py" --agents Cinco,Trip,Q > /tmp/heartbeat_adapter.log 2>&1 &
echo "Heartbeat adapter: $!"
ADAPTER_PID=$!

# Verify startup (2s grace period)
sleep 2
if kill -0 $ADAPTER_PID 2>/dev/null; then
    echo "=== Running (PID $ADAPTER_PID) ==="
else
    echo "=== FAILED — process exited immediately ==="
    tail -5 /tmp/heartbeat_adapter.log 2>/dev/null
    exit 1
fi
echo "Log: /tmp/heartbeat_adapter.log"
