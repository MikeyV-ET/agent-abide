#!/bin/bash
# Launch the Slack adapter, detached from any session.
# Usage: bash scripts/launch_slack_adapter.sh
#
# Kills existing instance first, then starts fresh.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMS="$SCRIPT_DIR/../core"

echo "=== Stopping existing Slack adapter ==="
pkill -f "slack_adapter.py" 2>/dev/null && echo "Killed Slack adapter" || echo "No Slack adapter running"
sleep 1

echo ""
echo "=== Starting Slack adapter ==="
setsid nohup python3 -u "$COMMS/slack_adapter.py" --agents Cinco > /tmp/slack_adapter.log 2>&1 &
echo "Slack adapter: $!"

echo ""
echo "=== Started ==="
echo "Log: /tmp/slack_adapter.log"
