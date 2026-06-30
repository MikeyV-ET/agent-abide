# interjection_hook.sh — sourced via BASH_ENV on every run_terminal_command
#
# Checks for queued interjection messages and prepends them to stdout,
# so they appear at the top of tool call output. Consumed files are deleted.
#
# Requires: $AGENT_NAME in environment (set by asdaaas.py)
# Queue dir: ~/agents/$AGENT_NAME/asdaaas/interjections/*.txt
#
# Performance: empty path is ~1ms (two builtins: test -d, compgen -G).
# No forks, no subshells, no external commands on the empty path.

# Guard: need AGENT_NAME to know which queue to check
[ -n "$AGENT_NAME" ] || return 0 2>/dev/null || true

_intj_dir="$HOME/agents/$AGENT_NAME/asdaaas/interjections"

# Fast path: bail if no directory or no .txt files (all builtins, no forks)
if [ -d "$_intj_dir" ] && compgen -G "$_intj_dir/*.txt" > /dev/null 2>&1; then

    # Stage files to a private temp dir for race safety.
    # mv is atomic per-file on Linux: if parallel shells race,
    # each file is consumed by exactly one shell.
    _intj_tmp=$(mktemp -d)
    if [ -d "$_intj_tmp" ]; then
        mv "$_intj_dir"/*.txt "$_intj_tmp/" 2>/dev/null

        # Verify we actually captured files (another shell may have raced us)
        if compgen -G "$_intj_tmp/*.txt" > /dev/null 2>&1; then
            echo "<interjection>"
            echo "[system: messages arrived during your tool call]"
            cat "$_intj_tmp"/*.txt
            echo "</interjection>"
        fi

        rm -rf "$_intj_tmp"
    fi
fi

unset _intj_dir _intj_tmp
