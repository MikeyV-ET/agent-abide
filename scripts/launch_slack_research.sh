#!/bin/bash
# Launch the Slack Research adapter, detached from any session.
# Usage: bash scripts/launch_slack_research.sh
#
# Kills existing instance first, then starts fresh.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMS="$SCRIPT_DIR/../core"

echo "=== Stopping existing Slack Research adapter ==="
pkill -f "slack_research_adapter.py" 2>/dev/null && echo "Killed Slack Research adapter" || echo "No Slack Research adapter running"
sleep 1

echo ""
echo "=== Starting Slack Research adapter ==="
setsid nohup python3 -u "$COMMS/slack_research_adapter.py" > /tmp/slack_research_adapter.log 2>&1 &
echo "Slack Research adapter: $!"

echo ""
echo "=== Started ==="
echo "Log: /tmp/slack_research_adapter.log"
