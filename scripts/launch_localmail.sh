#!/bin/bash
# Launch the localmail adapter, detached from any session.
# Usage: bash scripts/launch_localmail.sh
#
# Kills existing instance first, then starts fresh.
# Polls agent localmail inboxes and delivers as doorbells.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMMS="$SCRIPT_DIR/../core"

echo "=== Stopping existing localmail adapter ==="
pkill -f "localmail.py.*--poll" 2>/dev/null && echo "Killed localmail adapter" || echo "No localmail adapter running"
# Also match the watch_loop pattern and the new service name
pkill -f "python3.*localmail.py$" 2>/dev/null
pkill -f "python3.*localmail_service.py" 2>/dev/null
sleep 1

echo ""
echo "=== Starting localmail adapter ==="
setsid nohup python3 -u "$COMMS/localmail.py" --agents Sr,Jr,Trip,Q,Cinco,Squiggy,test-agent > /tmp/localmail_adapter.log 2>&1 &
echo "Localmail adapter: $!"
ADAPTER_PID=$!

# Verify startup (2s grace period)
sleep 2
if kill -0 $ADAPTER_PID 2>/dev/null; then
    echo "=== Running (PID $ADAPTER_PID) ==="
else
    echo "=== FAILED — process exited immediately ==="
    tail -5 /tmp/localmail_adapter.log 2>/dev/null
    exit 1
fi
echo "Log: /tmp/localmail_adapter.log"
