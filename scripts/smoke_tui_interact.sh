#!/usr/bin/env bash
# Drive real TUI; assert agent reply is visible on screen (user-visible path).
# Reply token is NOT present in the prompt (avoids false pass from input echo).
#
# ASDAAAS_CONFIG=/srv/config bash scripts/smoke_tui_interact.sh Wend [timeout]
set -euo pipefail
AGENT="${1:-Wend}"
TIMEOUT="${2:-120}"
OPERATOR="${PUPPET_OPERATOR:-Puppet}"
SESSION="tui-smoke-$(echo "$AGENT" | tr '[:upper:]' '[:lower:]')-$$"
AA_SCRIPTS="${AA_SCRIPTS:-/srv/agent-abide/scripts}"
CONFIG="${ASDAAAS_CONFIG:-/srv/config}/agents.json"
[ -d "${ASDAAAS_CONFIG:-}" ] && CONFIG="$ASDAAAS_CONFIG/agents.json"
HOME_A=$(python3 -c "import json;print(json.load(open('$CONFIG'))['agents']['$AGENT']['home'])")
CONV="$HOME_A/asdaaas/conversation.jsonl"

command -v tmux >/dev/null || { echo "FAIL: tmux required"; exit 2; }
ps -eo args | awk -v a="$AGENT" 'index($0,"asdaaas.py --agent "a)&&$0!~/awk/{f=1}END{exit f?0:1}' \
  || { echo "FAIL: asdaaas not running for $AGENT"; exit 2; }

# Token must not appear in the prompt string
A=$((17 + $(date +%s) % 5))   # 17..21
B=$((19 + $(date +%s) % 3))   # 19..21
EXPECT=$((A * B))
PROMPT="Smoke test: what is ${A} times ${B}? Reply with only the digits of the product, nothing else."
# sanity: expect not substring of prompt
echo "$PROMPT" | grep -q "$EXPECT" && { echo "FAIL: internal — expect leaked into prompt"; exit 2; }

cleanup() { tmux kill-session -t "$SESSION" 2>/dev/null || true; }
trap cleanup EXIT
tmux kill-session -t "$SESSION" 2>/dev/null || true

tmux new-session -d -s "$SESSION" -x 140 -y 42 -- \
  env ASDAAAS_CONFIG="${ASDAAAS_CONFIG:-/srv/config}" TERM=xterm-256color \
  bash "$AA_SCRIPTS/launch_tui.sh" --agent "$AGENT" --operator "$OPERATOR"

echo "waiting for TUI chrome…"
DEADLINE=$((SECONDS + 45))
while [ $SECONDS -lt $DEADLINE ]; do
  PANE=$(tmux capture-pane -t "$SESSION" -p -S -40 2>/dev/null || true)
  if echo "$PANE" | grep -qiE "$AGENT|ctx |❯"; then
    echo "PASS[chrome]: TUI up"
    break
  fi
  tmux has-session -t "$SESSION" 2>/dev/null || { echo "FAIL: TUI died"; exit 2; }
  sleep 1
done
echo "$PANE" | grep -qiE "$AGENT|ctx |❯" || { echo "FAIL[chrome]"; tmux capture-pane -t "$SESSION" -p -S -40; exit 2; }

BEFORE_SEQ=$(wc -l < "$CONV" 2>/dev/null || echo 0)
tmux send-keys -t "$SESSION" -l "$PROMPT"
sleep 0.4
tmux send-keys -t "$SESSION" Enter
echo "sent: $A x $B = $EXPECT (must appear as speech on pane)"

DEADLINE=$((SECONDS + TIMEOUT))
while [ $SECONDS -lt $DEADLINE ]; do
  SPOKE=0
  if [ -f "$CONV" ]; then
    python3 -c "
import json
from pathlib import Path
exp='$EXPECT'
for line in Path('$CONV').read_text().splitlines()[int('$BEFORE_SEQ'):]:
    try: o=json.loads(line)
    except: continue
    if o.get('role')=='assistant' and exp in (o.get('content') or ''):
        raise SystemExit(0)
raise SystemExit(1)
" 2>/dev/null && SPOKE=1
  fi

  PANE=$(tmux capture-pane -t "$SESSION" -p -S -120 2>/dev/null || true)
  # Drop bottom chrome (input + help); require expect in remaining body
  BODY=$(echo "$PANE" | head -n -4)
  VISIBLE=0
  # word-boundary-ish: line contains the number
  if echo "$BODY" | grep -E "(^|[^0-9])${EXPECT}([^0-9]|$)" >/dev/null; then
    VISIBLE=1
  fi

  if [ "$SPOKE" -eq 1 ] && [ "$VISIBLE" -eq 1 ]; then
    echo "PASS[interact]: assistant said $EXPECT and TUI body shows it"
    echo "$BODY" | tail -20
    exit 0
  fi
  if [ "$SPOKE" -eq 1 ] && [ "$VISIBLE" -eq 0 ]; then
    echo "… mind spoke $EXPECT; waiting for TUI paint…"
  fi
  tmux has-session -t "$SESSION" 2>/dev/null || { echo "FAIL: TUI died"; exit 2; }
  sleep 2
done

echo "FAIL[interact]: no spoken+visible product within ${TIMEOUT}s (expect $EXPECT)"
echo "--- conversation after send ---"
python3 -c "
import json
from pathlib import Path
p=Path('$CONV')
if p.exists():
  for line in p.read_text().splitlines()[int('$BEFORE_SEQ'):]:
    try: o=json.loads(line)
    except: continue
    print(o.get('role'), repr((o.get('content') or '')[:100]))
" 2>/dev/null || true
echo "--- pane ---"
tmux capture-pane -t "$SESSION" -p -S -80 || true
ls -la "$HOME_A/asdaaas/history/hot.jsonl" 2>&1 | head -1
exit 2
