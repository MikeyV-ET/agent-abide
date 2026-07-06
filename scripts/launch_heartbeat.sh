#!/bin/bash
# Launch the heartbeat adapter, detached from any session.
# Usage: bash scripts/launch_heartbeat.sh
#
# Kills existing instance first, then starts fresh.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMS="$SCRIPT_DIR/../core"

echo "=== Stopping existing heartbeat adapter ==="
pkill -f "heartbeat_adapter.py" 2>/dev/null && echo "Killed heartbeat adapter" || echo "No heartbeat adapter running"
sleep 1

echo ""
echo "=== Starting heartbeat adapter ==="
setsid nohup python3 -u "$COMMS/heartbeat_adapter.py" --agents Cinco,Trip,Q > /tmp/heartbeat_adapter.log 2>&1 &
echo "Heartbeat adapter: $!"

echo ""
echo "=== Started ==="
echo "Log: /tmp/heartbeat_adapter.log"
