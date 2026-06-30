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

# Only fire for bash -c invocations (actual tool call commands).
# Other bash processes (e.g. hook scripts run as "bash script.sh") also
# source BASH_ENV but should not consume interjection files.
case "$-" in *c*) ;; *) return 0 2>/dev/null || true ;; esac

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

            # Log delivery for diagnostics
            _intj_log="$HOME/agents/$AGENT_NAME/asdaaas/interjection_log.txt"
            _intj_count=$(ls -1 "$_intj_tmp"/*.txt 2>/dev/null | wc -l)
            _intj_snippet=$(head -c 200 "$_intj_tmp"/*.txt 2>/dev/null | tr '\n' ' ')
            echo "$(date '+%Y-%m-%d %H:%M:%S %Z') delivered=$_intj_count snippet=\"$_intj_snippet\"" >> "$_intj_log" 2>/dev/null
            unset _intj_log _intj_count _intj_snippet
        fi

        rm -rf "$_intj_tmp"
    fi
fi

unset _intj_dir _intj_tmp

# Prevent child processes from sourcing this hook again.
# Each new tool call gets a fresh BASH_ENV from the binary's env.
unset BASH_ENV
