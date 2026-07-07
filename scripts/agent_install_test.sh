#!/bin/bash
# Agent-driven installation test.
# Launches a fresh grok instance inside a container and asks it to
# read the repo docs and set up agent-abide from scratch.
#
# Usage:
#   bash scripts/agent_install_test.sh
#
# Requires: docker, ~/.grok/auth.json (for API access)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "=== Building container ==="
docker build -t agent-abide-agent-test "$REPO_DIR" 2>&1 | tail -3

PROMPT='You are a developer trying out agent-abide for the first time. You have the repo cloned at ~/agent-abide.

Your task:
1. Read the README.md to understand what this project is
2. Find and read any setup/getting-started documentation
3. Set up a new agent called "FreshAgent" following the documented steps
4. Try to launch the agent using the provided scripts
5. Report what happened at each step — what worked, what was confusing, what failed

You have all tools available. Work through this systematically. Do NOT skip steps or assume anything — follow the docs as written.

When done, write a file ~/install_report.txt summarizing:
- Each step you took
- Whether it succeeded or failed
- Any docs that were missing, wrong, or confusing
- Overall: could a developer get this running from the docs alone?'

echo ""
echo "=== Running agent install test ==="
echo "    (this may take a few minutes)"
echo ""

docker run --rm \
  -v "$HOME/.grok/auth.json:/tmp/host_auth.json:ro" \
  agent-abide-agent-test \
  bash -c "mkdir -p /home/testuser/.grok && cp /tmp/host_auth.json /home/testuser/.grok/auth.json && grok -p $(printf '%q' "$PROMPT") --yolo --max-turns 30 --cwd /home/testuser/agent-abide --output-format plain 2>&1; echo '---REPORT---'; cat /home/testuser/install_report.txt 2>/dev/null || echo 'No report generated'"
