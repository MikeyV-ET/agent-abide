# Binary state adapters (Grok / Claude)

Same pattern as stream history: **native → normalize → common machine**.

```text
Grok updates.jsonl  ── map_grok_updates_frame ──┐
                                                 ├→ ActivityEvent → BinaryActivityMachine → state file
Claude session.jsonl ─ map_claude_session_line ─┘
```

## Layout (`core/binary_state/`)

| File | Role |
|------|------|
| `types.py` | `ObserverState`, `ActivityKind`, `ActivityEvent` |
| `machine.py` | `BinaryActivityMachine` — BUSY/IDLE/STUCK/GATE/GONE |
| `grok.py` | **Done** — `map_grok_*`, `GrokBinaryStateObserver` |
| `claude.py` | **TODO (Opus)** — `map_claude_session_line`, `ClaudeBinaryStateObserver` |
| `service.py` | In-process / sidecar orchestration (grok path today) |
| `tail.py` | JSONL tailer |

Facade: `core/binary_state_observer.py` re-exports; `BinaryStateObserver` ≡ grok edge.

## Opus task: Claude mapper

1. Implement `map_claude_session_line(obj) -> ActivityEvent | None` in `claude.py`.
2. Use **only** `ActivityKind` values — do not invent grok `sessionUpdate` strings as kinds.
3. Set `source_type` to something useful for debug (`claude:assistant`, tool name, etc.).
4. Unit-test with a few Astro session lines from `~/.claude/projects/…`.
5. Do **not** change `BinaryActivityMachine` transitions without Trip-G/Eric.

### ActivityKind cheat sheet

| Kind | Meaning for machine |
|------|---------------------|
| `TURN_START` | BUSY, clear tools, new turn |
| `TURN_END` | IDLE |
| `SPEECH` / `THOUGHT` | ensure BUSY |
| `TOOL_START` | pending tool + silence window |
| `TOOL_END` | clear pending tool |
| `RETRYING` / `RETRY_FAILED` | retry states |
| `DOOM` | doom_loop flag |
| `UNKNOWN` | UNKNOWN state (avoid for normal traffic) |
| `KNOWN_OTHER` | counted activity, default silence |
| `MODEL_INFO` / `SESSION_ACTIVITY` | metadata only |

### Suggested Claude mapping

| Claude line | ActivityKind |
|-------------|--------------|
| `type=user` with user text | `TURN_START` |
| `type=user` tool_result only | `TOOL_END` (`tool_id=tool_use_id`) |
| `type=assistant` text blocks | `SPEECH` |
| `type=assistant` thinking | `THOUGHT` |
| `type=assistant` tool_use | `TOOL_START` (`id`, `name`) |
| chrome (attachment, …) | `None` (skip) |

Wire into `ClaudeBackend` later (not required for mapper PR).
