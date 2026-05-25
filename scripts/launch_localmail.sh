#!/bin/bash
# Launch the localmail adapter, detached from any session.
# Usage: bash ~/projects/mikeyv-infra/live/comms/launch_localmail.sh
#
# Kills existing instance first, then starts fresh.
# Polls agent localmail inboxes and delivers as doorbells.

COMMS=/home/eric/projects/mikeyv-infra/live/comms

echo "=== Stopping existing localmail adapter ==="
pkill -f "localmail.py.*--poll" 2>/dev/null && echo "Killed localmail adapter" || echo "No localmail adapter running"
# Also match the watch_loop pattern
pkill -f "python3.*localmail.py$" 2>/dev/null
sleep 1

echo ""
echo "=== Starting localmail adapter ==="
setsid nohup python3 -u "$COMMS/localmail.py" --agents Sr,Jr,Trip,Q,Cinco > /tmp/localmail_adapter.log 2>&1 &
echo "Localmail adapter: $!"

echo ""
echo "=== Started ==="
echo "Log: /tmp/localmail_adapter.log"
