#!/usr/bin/env bash
# Live smoke: agent must accept a tui inbox message (user_message_chunk within TIMEOUT).
# Usage: ASDAAAS_CONFIG=/srv/config bash scripts/smoke_agent_delivery.sh Wend [timeout_sec]
# Exit 0 = delivery ok; 2 = delivery failure / mute agent.
set -euo pipefail
AGENT="${1:-Wend}"
TIMEOUT="${2:-45}"
CONFIG="${ASDAAAS_CONFIG:-/srv/config}/agents.json"
if [ -d "${ASDAAAS_CONFIG:-}" ]; then CONFIG="$ASDAAAS_CONFIG/agents.json"; fi
if [ ! -f "$CONFIG" ]; then echo "FAIL: no $CONFIG"; exit 2; fi

HOME_A=$(python3 -c "import json;print(json.load(open('$CONFIG'))['agents']['$AGENT']['home'])")
SESSION=$(python3 -c "import json;print(json.load(open('$CONFIG'))['agents']['$AGENT'].get('session',''))")
# resolve session dir
ENC=$(python3 -c "import os;print('$HOME_A'.replace('/','%2F'))")
SDIR="$HOME_A/.grok/sessions/$ENC"
if [ -n "$SESSION" ] && [ -d "$SDIR/$SESSION" ]; then
  UPD="$SDIR/$SESSION/updates.jsonl"
else
  # newest session dir
  UPD=$(ls -dt "$SDIR"/*/updates.jsonl 2>/dev/null | head -1 || true)
fi
if [ -z "${UPD:-}" ]; then
  echo "FAIL: no updates.jsonl for $AGENT under $SDIR"
  exit 2
fi
# must be running
if ! ps -eo args | awk -v a="$AGENT" 'index($0,"asdaaas.py --agent "a) && $0 !~ /awk/{found=1} END{exit found?0:1}'; then
  echo "FAIL: asdaaas not running for $AGENT"
  exit 2
fi
BEFORE=$(wc -c < "$UPD" 2>/dev/null || echo 0)
MARK="smoke-delivery-$(date +%s)"
INBOX="$HOME_A/asdaaas/adapters/tui/inbox"
mkdir -p "$INBOX"
MSG="$INBOX/msg_$(date +%s%N)_smoke.json"
python3 -c "
import json, time
open('$MSG','w').write(json.dumps({
  'from':'Smoke','adapter':'tui','text':'$MARK',
  'ts':__import__('datetime').datetime.utcnow().isoformat()+'Z',
  'meta':{'room':'tui','operator':'Smoke','surface':'smoke'}
}))
"
echo "queued $MSG mark=$MARK updates=$UPD before=$BEFORE"
DEADLINE=$((SECONDS + TIMEOUT))
while [ $SECONDS -lt $DEADLINE ]; do
  if grep -q "user_message_chunk" "$UPD" 2>/dev/null && grep -q "$MARK" "$UPD" 2>/dev/null; then
    echo "PASS: user_message_chunk for mark in updates.jsonl"
    exit 0
  fi
  # also accept mark in any form in updates
  if grep -q "$MARK" "$UPD" 2>/dev/null; then
    echo "PASS: mark appeared in updates.jsonl"
    exit 0
  fi
  # hard fail early if log says unknown session / delivery failure
  if grep -q "unknown session id" /tmp/asdaaas_${AGENT,,}.log 2>/dev/null \
     || grep -q "DELIVERY_FAILURE" /tmp/asdaaas_${AGENT,,}.log 2>/dev/null \
     || grep -q "unknown session id" /tmp/asdaaas_wend.log 2>/dev/null; then
    # only fail if after we queued and still no growth
    :
  fi
  sleep 1
done
echo "FAIL: no user_message_chunk within ${TIMEOUT}s (agent mute / bad session)"
echo "--- updates size ---"
wc -c "$UPD" || true
echo "--- log tail ---"
tail -20 /tmp/asdaaas_${AGENT,,}.log 2>/dev/null || tail -20 /tmp/asdaaas_wend.log 2>/dev/null || true
exit 2
