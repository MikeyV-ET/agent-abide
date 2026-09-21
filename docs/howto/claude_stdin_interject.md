# Claude mid-turn stdin interject

Proven by `scripts/probe_claude_stdin_interject.py` (Astro ddb0e7e).

## Runtime wire (Trip-G)

When `interjection_enabled` (agents.json) is true for a Claude agent:

1. `ClaudeBackend.start` adds `--replay-user-messages`
2. `interjection_watcher` calls `backend.inject_user_message(text)` during collect
3. On success, skip BASH_ENV disk queue (no double delivery)
4. On inject failure, fall back to `queue_interjection` + BASH_ENV

Claude holds the injected message until the running tool returns, then delivers
it in the **same turn** (not mid-tool preemption).

## TUI

Replayed `type:user` frames appear via hot path. Discriminate with
`ClaudeBackend.was_injected(exact_text)` if needed. Formatted text already
includes `[operator (via tui) (id=…)]`.

## Operator (user) name

TUI: `--operator NAME` / `-o`, `/whoami`, or first-launch OperatorScreen.
Saved in `~/.config/abidetui/operator.json`. Inbox messages use
`"from": operator`.
