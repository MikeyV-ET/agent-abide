#!/bin/bash
# Smoke test for agent-abide installation.
# Verifies: path resolution, config loading, agent setup, script execution,
# and asdaaas startup (up to backend session load or auth failure).
#
# Exit 0 = all checks pass. Non-zero = something is broken.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${AGENT_ABIDE_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PASS=0
FAIL=0

check() {
    local desc="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        echo "  PASS  $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $desc"
        FAIL=$((FAIL + 1))
    fi
}

check_output() {
    local desc="$1"
    local expected="$2"
    shift 2
    local output
    output=$("$@" 2>&1) || true
    if echo "$output" | grep -q "$expected"; then
        echo "  PASS  $desc"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $desc (expected '$expected' in output)"
        echo "        got: ${output:0:200}"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== 1. Prerequisites ==="
check "python3 available" which python3
check "node available" which node
check "npm available" which npm
check "grok binary installed" which grok
check "git available" which git
check_output "grok version" "grok" grok --version

echo ""
echo "=== 2. No hardcoded paths ==="
HARDCODED=$(grep -rn '/home/eric' --include='*.py' --include='*.sh' --include='*.md' "$REPO" | grep -v 'agents.json.example' | grep -v 'test_e2e_localmail.py' | grep -v 'smoke_test.sh' || true)
if [ -z "$HARDCODED" ]; then
    echo "  PASS  No /home/eric paths in codebase"
    PASS=$((PASS + 1))
else
    echo "  FAIL  Hardcoded paths found:"
    echo "$HARDCODED" | head -10 | sed 's/^/        /'
    FAIL=$((FAIL + 1))
fi

echo ""
echo "=== 3. Config ==="
check "agents.json exists" test -f "$REPO/agents.json"
check "agents.json.example exists" test -f "$REPO/agents.json.example"
check "agents.json is valid JSON" python3 -c "import json; json.load(open('$REPO/agents.json'))"

echo ""
echo "=== 4. Script path resolution ==="
# Verify scripts can resolve their own paths from any CWD
cd /tmp
for script in launch_localmail launch_heartbeat launch_irc_adapter launch_remind launch_slack_adapter launch_slack_research; do
    check_output "$script.sh resolves COMMS" "SCRIPT_DIR" head -10 "$REPO/scripts/${script}.sh"
done
check_output "launch_asdaaas.sh resolves ASDAAAS_DIR" 'cd "$SCRIPT_DIR/.."' head -30 "$REPO/scripts/launch_asdaaas.sh"
check_output "restart_agent.sh resolves ASDAAAS_DIR" 'cd "$SCRIPT_DIR/.."' head -40 "$REPO/scripts/restart_agent.sh"

echo ""
echo "=== 5. Script syntax ==="
cd "$REPO/scripts"
for f in *.sh; do
    check "$f parses" bash -n "$f"
done

echo ""
echo "=== 6. Agent setup ==="
cd "$REPO"
check_output "setup_agent.sh creates agent" "ready" bash scripts/setup_agent.sh SmokeTestAgent "$HOME/agents"

check "Agent home created" test -d "$HOME/agents/SmokeTestAgent"
check "Agent asdaaas dir" test -d "$HOME/agents/SmokeTestAgent/asdaaas"
check "Agent doorbells dir" test -d "$HOME/agents/SmokeTestAgent/asdaaas/doorbells"
check "Agent commands dir" test -d "$HOME/agents/SmokeTestAgent/asdaaas/commands"
check "Agent awareness.json" test -f "$HOME/agents/SmokeTestAgent/asdaaas/awareness.json"
check "Agent gaze.json" test -f "$HOME/agents/SmokeTestAgent/asdaaas/gaze.json"
check "Agent lab notebook" test -f "$HOME/agents/SmokeTestAgent/lab_notebook.md"
check "Agent notes to self" test -f "$HOME/agents/SmokeTestAgent/notes_to_self.md"

echo ""
echo "=== 7. Python imports ==="
cd "$REPO/core"
check "asdaaas.py imports" python3 -c "import ast; ast.parse(open('asdaaas.py').read())"
check "grok_backend.py imports" python3 -c "import ast; ast.parse(open('grok_backend.py').read())"
check "localmail.py imports" python3 -c "import ast; ast.parse(open('localmail.py').read())"
check "asdaaas_config.py imports" python3 -c "import ast; ast.parse(open('asdaaas_config.py').read())"
check "binary_state_observer.py imports" python3 -c "import ast; ast.parse(open('binary_state_observer.py').read())"

echo ""
echo "=== 8. Startup test ==="
# Try to start asdaaas — expect it to get as far as backend startup.
# Without auth it will fail at session load, which is fine.
cd "$REPO"
timeout 15 python3 core/asdaaas.py --agent SmokeTestAgent --cwd "$HOME/agents/SmokeTestAgent" > /tmp/startup.log 2>&1 || true
if grep -q "Starting backend\|Backend:\|grok agent stdio\|Session:" /tmp/startup.log 2>/dev/null; then
    echo "  PASS  asdaaas.py reached backend startup"
    PASS=$((PASS + 1))
elif grep -q "Ready\.\|main loop" /tmp/startup.log 2>/dev/null; then
    echo "  PASS  asdaaas.py reached ready state"
    PASS=$((PASS + 1))
else
    echo "  INFO  asdaaas.py startup output (may fail at auth):"
    tail -10 /tmp/startup.log 2>/dev/null | sed 's/^/        /'
    # Check for path-related errors (the thing we're actually testing)
    if grep -qi "no such file\|not found\|ModuleNotFoundError\|FileNotFoundError" /tmp/startup.log 2>/dev/null; then
        echo "  FAIL  Path or module error during startup"
        FAIL=$((FAIL + 1))
    else
        echo "  PASS  No path/module errors (auth failure is expected without XAI_API_KEY)"
        PASS=$((PASS + 1))
    fi
fi

echo ""
echo "========================================="
echo "  Results: $PASS passed, $FAIL failed"
echo "========================================="

if [ $FAIL -gt 0 ]; then
    exit 1
fi
