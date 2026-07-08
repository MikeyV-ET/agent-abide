#!/bin/bash
# start-aa.sh — Start agent-abide. That's it.
#
# The agent already set everything up. This just brings it back online.
# Usage: ./start-aa.sh [--config /path/to/config/dir]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Parse --config
CONFIG_DIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) CONFIG_DIR="$2"; shift 2 ;;
        *) shift ;;
    esac
done

# Export config dir so all scripts and Python pick it up
if [[ -n "$CONFIG_DIR" ]]; then
    export ASDAAAS_CONFIG="$CONFIG_DIR"
elif [[ -n "$ASDAAAS_CONFIG" ]]; then
    CONFIG_DIR="$ASDAAAS_CONFIG"
fi

# Quick sanity check
CONFIG_FILE="${CONFIG_DIR:+$CONFIG_DIR/}agents.json"
if [[ -z "$CONFIG_DIR" ]]; then
    CONFIG_FILE="$SCRIPT_DIR/agents.json"
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "No agents.json found at $CONFIG_FILE"
    echo "Run setup first, or pass --config /path/to/config/dir"
    exit 1
fi

echo "Starting agent-abide..."
bash "$SCRIPT_DIR/scripts/launch_asdaaas.sh" --wait

echo ""
echo "Agents are up. To open the dashboard:"
echo "  bash $SCRIPT_DIR/scripts/launch_tui.sh"
