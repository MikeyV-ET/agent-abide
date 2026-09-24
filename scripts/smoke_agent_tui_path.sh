#!/usr/bin/env bash
# Full TUI-path smoke: what "citizen up + TUI works" must mean.
#
# Catches the Sep 24 failure class:
#   - asdaaas running but grok mute (no delivery)
#   - delivery OK but TUI blank (no hot.jsonl / missing zstandard / hot ingest off)
#
# Usage:
#   ASDAAAS_CONFIG=/srv/config bash scripts/smoke_agent_tui_path.sh Wend [timeout_sec]
#
# Exit 0 = all gates pass; 2 = fail (prints which gate).
set -euo pipefail
AGENT="${1:-Wend}"
TIMEOUT="${2:-60}"
CONFIG="${ASDAAAS_CONFIG:-/srv/config}/agents.json"
if [ -d "${ASDAAAS_CONFIG:-}" ]; then CONFIG="$ASDAAAS_CONFIG/agents.json"; fi
if [ ! -f "$CONFIG" ]; then echo "FAIL[config]: no $CONFIG"; exit 2; fi

HOME_A=$(python3 -c "import json;print(json.load(open('$CONFIG'))['agents']['$AGENT']['home'])")
HIST="$HOME_A/asdaaas/history"
HOT="$HIST/hot.jsonl"
HCFG="$HIST/config.json"
CONV="$HOME_A/asdaaas/conversation.jsonl"
HEALTH="$HOME_A/asdaaas/health.json"
FAILS=0

fail() { echo "FAIL[$1]: $2"; FAILS=$((FAILS+1)); }
pass() { echo "PASS[$1]: $2"; }

# --- Gate 0: process ---
if ! ps -eo args | awk -v a="$AGENT" 'index($0,"asdaaas.py --agent "a) && $0 !~ /awk/{found=1} END{exit found?0:1}'; then
  fail process "asdaaas not running for $AGENT"
else
  pass process "asdaaas running"
fi

# --- Gate 1: zstandard (hot L1 codec) ---
if python3 -c "import zstandard" 2>/dev/null; then
  pass zstandard "importable"
else
  fail zstandard "python3 cannot import zstandard (hot ingest dies silently for TUI paint)"
fi

# --- Gate 2: hot ingest config ---
if [ -f "$HCFG" ] && python3 -c "
import json,sys
c=json.load(open('$HCFG'))
sys.exit(0 if (c.get('tail_stream') or c.get('tail_backend') or c.get('tail_grok')) else 1)
"; then
  pass hot_config "history/config.json enables tail_stream"
else
  fail hot_config "missing or disabled $HCFG (aa-dev TUI will not paint)"
fi

# --- Gate 3: live session ---
SESSION=$(python3 -c "
import json
from pathlib import Path
h=Path('$HEALTH')
print(json.loads(h.read_text()).get('session_id','') if h.is_file() else '')
" 2>/dev/null || true)
if [ -z "$SESSION" ]; then
  fail session "no session_id in health.json"
else
  pass session "session_id=$SESSION"
fi

ENC=$(python3 -c "print('$HOME_A'.replace('/','%2F'))")
UPD="$HOME_A/.grok/sessions/$ENC/$SESSION/updates.jsonl"
if [ ! -f "$UPD" ]; then
  UPD=$(ls -dt "$HOME_A/.grok/sessions/$ENC"/*/updates.jsonl 2>/dev/null | head -1 || true)
fi

# --- Gate 4+5: delivery + hot growth ---
MARK="tui-path-smoke-$(date +%s)"
INBOX="$HOME_A/asdaaas/adapters/tui/inbox"
mkdir -p "$INBOX"
BEFORE_HOT=0
[ -f "$HOT" ] && BEFORE_HOT=$(wc -c < "$HOT" || echo 0)
BEFORE_UPD=0
[ -f "$UPD" ] && BEFORE_UPD=$(wc -c < "$UPD" || echo 0)

MSG="$INBOX/msg_$(date +%s%N)_pathsmoke.json"
python3 -c "
import json
open('$MSG','w').write(json.dumps({
  'from':'Smoke','adapter':'tui','text':'$MARK',
  'ts':__import__('datetime').datetime.utcnow().isoformat()+'Z',
  'meta':{'room':'tui','operator':'Smoke','surface':'tui-path-smoke'}
}))
"
chown -R --reference="$HOME_A/asdaaas" "$INBOX" 2>/dev/null || true
echo "queued mark=$MARK hot_before=$BEFORE_HOT upd_before=$BEFORE_UPD"

DELIVERED=0
HOT_GREW=0
DEADLINE=$((SECONDS + TIMEOUT))
while [ $SECONDS -lt $DEADLINE ]; do
  if [ "$DELIVERED" -eq 0 ]; then
    if { [ -n "${UPD:-}" ] && [ -f "$UPD" ] && grep -q "$MARK" "$UPD" 2>/dev/null; } \
       || { [ -f "$CONV" ] && grep -q "$MARK" "$CONV" 2>/dev/null; }; then
      DELIVERED=1
      pass delivery "mark reached updates/conversation"
    fi
  fi
  if [ "$HOT_GREW" -eq 0 ] && [ -f "$HOT" ]; then
    NOW=$(wc -c < "$HOT" || echo 0)
    if [ "$NOW" -gt "$BEFORE_HOT" ]; then
      HOT_GREW=1
      pass hot_grow "hot.jsonl grew ${BEFORE_HOT} -> ${NOW} bytes"
    fi
  fi
  if [ "$DELIVERED" -eq 1 ] && [ "$HOT_GREW" -eq 1 ]; then
    break
  fi
  # hard fail signals in log
  if grep -q "requires 'zstandard'" /tmp/asdaaas_wend.log 2>/dev/null \
     || grep -q "requires 'zstandard'" /tmp/asdaaas_${AGENT,,}.log 2>/dev/null; then
    fail zstandard_runtime "log shows hot ingest blocked on zstandard"
    break
  fi
  sleep 1
done

[ "$DELIVERED" -eq 1 ] || fail delivery "no mark in updates/conversation within ${TIMEOUT}s (mute agent)"
[ -f "$HOT" ] || fail hot_exists "hot.jsonl still missing after message (TUI will stay blank)"
[ "$HOT_GREW" -eq 1 ] || fail hot_grow "hot.jsonl did not grow after message (TUI paint stalled)"

# --- Gate 6: health not stuck STARTING forever ---
if [ -f "$HEALTH" ]; then
  python3 -c "
import json,sys
h=json.load(open('$HEALTH'))
obs=(h.get('observer') or {})
st=obs.get('state') or ''
if st=='STARTING' and float(h.get('totalTokens') or 0)==0:
  print('FAIL[health]: observer STARTING with 0 tokens')
  sys.exit(1)
print('PASS[health]: status=%s observer=%s tokens=%s' % (h.get('status'), st, h.get('totalTokens')))
" || FAILS=$((FAILS+1))
fi

echo "=== summary fails=$FAILS ==="
[ "$FAILS" -eq 0 ] && exit 0 || exit 2
