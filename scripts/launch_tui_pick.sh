#!/usr/bin/env bash
# Launch TUI with explicit agent pick (no default).
# If --agent/-a already passed, delegates to launch_tui.sh.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${ASDAAAS_CONFIG:-$HOME/agents/config}/agents.json"
[[ -f "$CONFIG" ]] || CONFIG="/srv/config/agents.json"

if [[ " $* " =~ " --agent " ]] || [[ " $* " =~ " -a " ]]; then
  exec bash "$SCRIPT_DIR/launch_tui.sh" "$@"
fi

echo "No --agent given. Citizens (no default):"
if [[ -f "$CONFIG" ]]; then
  mapfile -t NAMES < <(python3 -c "import json; d=json.load(open('$CONFIG')); print('\n'.join(sorted((d.get('agents') or {}))))")
else
  NAMES=(Jr Sr Cinco Trip Q Wend Squiggy)
fi
i=1
for n in "${NAMES[@]}"; do
  printf "  %2d) %s\n" "$i" "$n"
  i=$((i+1))
done
echo -n "Pick number or name: "
read -r choice
if [[ "$choice" =~ ^[0-9]+$ ]]; then
  idx=$((choice-1))
  AGENT="${NAMES[$idx]:-}"
else
  AGENT="$choice"
fi
if [[ -z "${AGENT:-}" ]]; then
  echo "No agent selected."
  exit 1
fi
exec bash "$SCRIPT_DIR/launch_tui.sh" --agent "$AGENT" "$@"
