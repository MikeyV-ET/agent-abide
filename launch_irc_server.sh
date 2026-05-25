#!/bin/bash
# Launch miniircd IRC server on localhost:6667.
# Usage: bash ~/projects/mikeyv-infra/live/comms/launch_irc_server.sh
#
# Kills existing instance first, then starts fresh.

echo "=== Stopping existing miniircd ==="
pkill -f miniircd 2>/dev/null && echo "Killed miniircd" || echo "No miniircd running"
sleep 1

echo ""
echo "=== Starting miniircd ==="
mkdir -p ~/.grok/irc_logs

setsid nohup python3 ~/.local/bin/miniircd \
  --listen 127.0.0.1 --ports 6667 \
  --channel-log-dir ~/.grok/irc_logs --verbose \
  > /tmp/miniircd.log 2>&1 &
echo "miniircd: $!"

echo ""
echo "=== Started ==="
echo "Log: /tmp/miniircd.log"
echo "Connect: irssi -c 127.0.0.1 -p 6667 -n eric"
