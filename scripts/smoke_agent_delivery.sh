#!/usr/bin/env bash
# Live smoke: agent must accept a tui inbox message within TIMEOUT.
# Exit 0 = delivery ok; 2 = mute / failure.
set -euo pipefail
AGENT="${1:-Wend}"
TIMEOUT="${2:-60}"
CONFIG="${ASDAAAS_CONFIG:-/srv/config}/agents.json"
if [ -d "${ASDAAAS_CONFIG:-}" ]; then CONFIG="$ASDAAAS_CONFIG/agents.json"; fi
if [ ! -f "$CONFIG" ]; then echo "FAIL: no $CONFIG"; exit 2; fi

HOME_A=$(python3 -c "import json;print(json.load(open('$CONFIG'))['agents']['$AGENT']['home'])")
# Roster session is the spine. Do not follow health.json (forks).
SESSION=$(python3 -c "import json;print(json.load(open('$CONFIG'))['agents']['$AGENT'].get('session',''))")
ENC=$(python3 -c "print('$HOME_A'.replace('/','%2F'))")
SDIR="$HOME_A/.grok/sessions/$ENC"
if [ -n "$SESSION" ] && [ -f "$SDIR/$SESSION/updates.jsonl" ]; then
  UPD="$SDIR/$SESSION/updates.jsonl"
else
  UPD=$(ls -dt "$SDIR"/*/updates.jsonl 2>/dev/null | head -1 || true)
fi
CONV="$HOME_A/asdaaas/conversation.jsonl"

if ! ps -eo args | awk -v a="$AGENT" 'index($0,"asdaaas.py --agent "a) && $0 !~ /awk/{found=1} END{exit found?0:1}'; then
  echo "FAIL: asdaaas not running for $AGENT"
  exit 2
fi

MARK="smoke-delivery-$(date +%s)"
INBOX="$HOME_A/asdaaas/adapters/tui/inbox"
mkdir -p "$INBOX"
MSG="$INBOX/msg_$(date +%s%N)_smoke.json"
python3 -c "
import json
open('$MSG','w').write(json.dumps({
  'from':'Smoke','adapter':'tui','text':'$MARK',
  'ts':__import__('datetime').datetime.utcnow().isoformat()+'Z',
  'meta':{'room':'tui','operator':'Smoke','surface':'smoke'}
}))
"
# ensure agent can read (glass often writes as operator)
chown -R --reference="$HOME_A/asdaaas" "$INBOX" 2>/dev/null || true

echo "queued mark=$MARK session=$SESSION updates=${UPD:-none}"
DEADLINE=$((SECONDS + TIMEOUT))
while [ $SECONDS -lt $DEADLINE ]; do
  if [ -n "${UPD:-}" ] && grep -q "$MARK" "$UPD" 2>/dev/null; then
    echo "PASS: mark in updates.jsonl"
    exit 0
  fi
  if [ -f "$CONV" ] && grep -q "$MARK" "$CONV" 2>/dev/null; then
    echo "PASS: mark in conversation.jsonl (TUI path)"
    exit 0
  fi
  sleep 1
done
echo "FAIL: no delivery within ${TIMEOUT}s"
wc -c "${UPD:-/dev/null}" "$CONV" 2>/dev/null || true
tail -15 /tmp/asdaaas_wend.log 2>/dev/null || true
exit 2
