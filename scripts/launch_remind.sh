#!/bin/bash
# Launch the remind adapter, detached from any session.
# Usage: bash scripts/launch_remind.sh
#
# Kills existing instance first, then starts fresh.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMS="$SCRIPT_DIR/../core"

echo "=== Stopping existing remind adapter ==="
pkill -f "remind_adapter.py" 2>/dev/null && echo "Killed remind adapter" || echo "No remind adapter running"
sleep 1

echo ""
echo "=== Starting remind adapter ==="
setsid nohup python3 -u "$COMMS/remind_adapter.py" > /tmp/remind_adapter.log 2>&1 &
echo "Remind adapter: $!"

echo ""
echo "=== Started ==="
echo "Log: /tmp/remind_adapter.log"
