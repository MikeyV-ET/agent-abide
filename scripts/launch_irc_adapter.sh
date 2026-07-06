#!/bin/bash
# Launch the IRC adapter, detached from any session.
# Usage: bash scripts/launch_irc_adapter.sh
#
# Kills existing instance first, then starts fresh.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMS="$SCRIPT_DIR/../core"

echo "=== Stopping existing IRC adapter ==="
pkill -f "irc_adapter.py" 2>/dev/null && echo "Killed IRC adapter" || echo "No IRC adapter running"
sleep 1

echo ""
echo "=== Starting IRC adapter ==="
setsid nohup python3 -u "$COMMS/irc_adapter.py" > /tmp/irc_adapter.log 2>&1 &
echo "IRC adapter: $!"

echo ""
echo "=== Started ==="
echo "Log: /tmp/irc_adapter.log"
