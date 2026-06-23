#!/usr/bin/env bash
# Test: restart_agent.sh session check should find "Session:" in new process output
# Bug: sometimes reports FAIL even though agent starts successfully
# 
# Simulates the race condition: old process shutdown lines written to log,
# then new process startup lines appended. The wait_for_log function
# (using tail -n +N | grep -q) should find "Session:" in the new lines.
#
# Run: bash tests/test_restart_session_check.sh

set -euo pipefail

TMPLOG=$(mktemp /tmp/test_restart_log.XXXXXX)
trap "rm -f $TMPLOG" EXIT

# Simulate old process log output (already in log before restart)
cat > "$TMPLOG" << 'EOF'
[asdaaas] ASDAAAS v2 starting for TestAgent (code: abc1234)
[asdaaas] Backend: grok
[asdaaas] Starting backend: GrokBackend
[asdaaas] PID 12345
[asdaaas] Session: 019de4e1-old-session
[asdaaas] Ready.
[asdaaas] Polling for 'TestAgent'...
EOF

# Simulate shutdown lines (written by old process during stop stage)
cat >> "$TMPLOG" << 'EOF'
[asdaaas] Command: shutdown (req=)
[asdaaas] Shutdown command received for TestAgent
[asdaaas] Shutting down TestAgent gracefully
[asdaaas] Unregistered TestAgent from running_agents.json
[asdaaas] TestAgent shut down.
EOF

# Record LOG_START_LINE (this is what the restart script does in stage_launch)
LOG_START_LINE=$(wc -l < "$TMPLOG")
LOG_START_LINE=$((LOG_START_LINE + 1))

# Simulate new process startup (appended after a short delay, like setsid nohup)
(sleep 2; cat >> "$TMPLOG" << 'EOF'
[asdaaas] ASDAAAS v2 starting for TestAgent (code: def5678)
[asdaaas] Backend: grok
[asdaaas] Starting backend: GrokBackend
[asdaaas] PID 67890
[asdaaas] Session: 019de4e1-new-session
[asdaaas] Ready.
EOF
) &

# This is the wait_for_log function from restart_agent.sh
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

echo "LOG_START_LINE=$LOG_START_LINE"
echo "Testing: wait_for_log should find 'Session:' in new process output within 10s..."

if wait_for_log "$TMPLOG" "Session:" 10; then
    echo "PASS: Found 'Session:' in new output"
    # Verify it found the NEW session, not the old one
    found=$(tail -n +${LOG_START_LINE} "$TMPLOG" | grep "Session:" | head -1)
    echo "  Found line: $found"
    if echo "$found" | grep -q "new-session"; then
        echo "PASS: Correctly found NEW session"
    else
        echo "FAIL: Found OLD session line instead of new one"
        exit 1
    fi
else
    echo "FAIL: 'Session:' not found within 10s (false negative)"
    echo "  Lines from LOG_START_LINE=$LOG_START_LINE:"
    tail -n +${LOG_START_LINE} "$TMPLOG" 2>/dev/null || echo "  (no lines)"
    exit 1
fi
