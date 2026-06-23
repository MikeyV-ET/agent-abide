#!/usr/bin/env bash
# Test: restart_agent.sh session check timeout false negative (issue_0026)
#
# Bug: restart_agent.sh stage_session uses wait_for_log("Session:", 30).
# When grok binary resumes a large session (5247 entries), session load
# takes >30s. "Session:" appears in log at ~35s, but the 30s timeout
# already returned FAIL. The agent is actually fine.
#
# This test reproduces the bug by launching asdaaas with MockBinary
# (startup_delay=8s) and running wait_for_log with a 5s timeout.
# The session line appears at ~8s but the 5s check times out — false negative.
#
# Run: bash tests/test_restart_session_check.sh
# Requires: MockBinary (~/projects/agent-abide/core/mock_binary.py)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_DIR="$SCRIPT_DIR/../core"
TMPLOG=$(mktemp /tmp/test_restart_log.XXXXXX)
AGENT_NAME="RestartTestAgent"
AGENT_HOME="$HOME/agents/$AGENT_NAME"

cleanup() {
    rm -f "$TMPLOG"
    # Kill any leftover test process
    pkill -f "asdaaas.py --agent $AGENT_NAME" 2>/dev/null || true
    # Clean up agent dirs
    rm -rf "$AGENT_HOME"
}
trap cleanup EXIT

# Create minimal agent workspace
mkdir -p "$AGENT_HOME/asdaaas/"{doorbells,commands,adapters/tui/{inbox,outbox},adapters/localmail/{payloads,inbox},adapters/remind/inbox,profile}

# Write awareness and gaze
cat > "$AGENT_HOME/asdaaas/awareness.json" << 'EOF'
{"direct_attach": ["tui"], "control_watch": {}, "notify_watch": [], "accept_from": ["*"], "default_doorbell": true, "doorbell_ttl": {"default": 3}}
EOF
cat > "$AGENT_HOME/asdaaas/gaze.json" << 'EOF'
{"speech": {"target": "tui", "params": {}}, "thoughts": null}
EOF
echo "# RestartTestAgent" > "$AGENT_HOME/AGENTS.md"

# Ensure agent is in agents.json
python3 -c "
import json, os
config_path = '$CORE_DIR/../agents.json'
with open(config_path) as f:
    config = json.load(f)
agents = config.get('agents', {})
if '$AGENT_NAME' not in agents:
    agents['$AGENT_NAME'] = {'home': '$AGENT_HOME', 'backend': 'grok', 'yolo': True}
    config['agents'] = agents
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)
    print('Added $AGENT_NAME to agents.json')
else:
    print('$AGENT_NAME already in agents.json')
"

# Simulate old process log output (pre-restart)
cat > "$TMPLOG" << 'EOF'
[asdaaas] ASDAAAS v2 starting for RestartTestAgent (code: abc1234)
[asdaaas] Backend: grok
[asdaaas] Starting backend: GrokBackend
[asdaaas] PID 12345
[asdaaas] Session: 019de4e1-old-session
[asdaaas] Ready.
[asdaaas] Polling for 'RestartTestAgent'...
EOF

# Record LOG_START_LINE (same as restart_agent.sh stage_launch)
LOG_START_LINE=$(wc -l < "$TMPLOG")
LOG_START_LINE=$((LOG_START_LINE + 1))

# wait_for_log — copied from restart_agent.sh
wait_for_log() {
    local log_file="$1"
    local pattern="$2"
    local timeout_secs="$3"
    local elapsed=0
    while [ $elapsed -lt $timeout_secs ]; do
        if [ -f "$log_file" ] && tail -n +${LOG_START_LINE:-1} "$log_file" 2>/dev/null | grep -q "$pattern"; then
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    return 1
}

# Launch asdaaas with MockBinary(startup_delay=8s)
# This simulates a large session load that takes 8s
echo "Launching asdaaas with MockBinary (startup_delay=8s)..."
setsid python3 -u -c "
import sys, asyncio
sys.path.insert(0, '$CORE_DIR')
from mock_binary import MockBinary, NormalResponse, EmptyResponse
from asdaaas import main
import asdaaas
asdaaas._shutdown_requested = False

scenario = [EmptyResponse(tokens=5000)]
mock = MockBinary(scenario, startup_delay=8.0)

async def run():
    try:
        await asyncio.wait_for(
            main('$AGENT_NAME', backend=mock, agent_cwd='$AGENT_HOME'),
            timeout=20,
        )
    except (asyncio.TimeoutError, SystemExit):
        pass

asyncio.run(run())
" >> "$TMPLOG" 2>&1 &
LAUNCH_PID=$!

echo "LOG_START_LINE=$LOG_START_LINE, PID=$LAUNCH_PID"

# ---- TEST 1: Short timeout (5s) should FAIL (reproduces the bug) ----
echo ""
echo "TEST 1: wait_for_log('Session:', 5) — should FAIL (timeout < startup delay)"
if wait_for_log "$TMPLOG" "Session:" 5; then
    echo "  UNEXPECTED PASS: Found 'Session:' within 5s (startup_delay=8s should prevent this)"
    echo "  This means the bug is NOT reproduced."
    exit 1
else
    echo "  PASS: Correctly timed out — false negative reproduced!"
    echo "  (restart_agent.sh would report FAIL here even though agent is loading)"
fi

# ---- TEST 2: Long timeout (15s) should PASS (agent does start eventually) ----
echo ""
echo "TEST 2: wait_for_log('Session:', 15) — should PASS (timeout > startup delay)"
if wait_for_log "$TMPLOG" "Session:" 15; then
    echo "  PASS: Found 'Session:' with longer timeout"
    found=$(tail -n +${LOG_START_LINE} "$TMPLOG" | grep "Session:" | head -1)
    echo "  Found line: $found"
else
    echo "  FAIL: 'Session:' not found even with 15s timeout"
    echo "  Last 20 lines of log:"
    tail -20 "$TMPLOG" | sed 's/^/    /'
    exit 1
fi

# Clean up the background process
kill $LAUNCH_PID 2>/dev/null || true
wait $LAUNCH_PID 2>/dev/null || true

echo ""
echo "=== All tests passed ==="
echo "Bug reproduced: wait_for_log with 30s timeout fails when session load takes >30s."
echo "Fix needed: increase STARTUP_TIMEOUT or make stage_session timeout configurable."
