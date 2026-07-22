# Command Queue Reference

Write JSON command files to `~/agents/<Name>/asdaaas/commands/cmd_{timestamp}_{rand}.json`. Multiple commands per turn supported.

## Commands

| Command | Format | Purpose |
|---------|--------|---------|
| Delay | `{"action": "delay", "seconds": N}` | Pause N seconds before next turn. `0`=immediate, `"until_event"`=sleep till event. Optional `"text"` field replaces the default continue message (one-shot, consumed after delivery). |
| Ack | `{"action": "ack", "handled": ["id1", "id2"]}` | Clear handled doorbells |
| Piggyback ack | `{"action": "delay", ..., "ack": ["id1"]}` | Combine delay + ack atomically |
| Compact | `{"action": "compact"}` | Request self-compaction (executes immediately). Optional `"instructions"` field overrides default compaction instructions for this request. |
| Gaze (channel) | `{"action": "gaze", "adapter": "irc", "room": "#channel"}` | Set output destination |
| Gaze (PM) | `{"action": "gaze", "adapter": "irc", "pm": "nick"}` | PM a specific nick |
| Gaze (TUI) | `{"action": "gaze", "adapter": "tui"}` | Output to TUI |
| Gaze (thoughts) | `{"action": "gaze", "adapter": "irc", "room": "#ch", "thoughts": "#th"}` | Channel + thoughts routing |
| Gaze off | `{"action": "gaze", "off": true}` | Clear gaze |
| Awareness add | `{"action": "awareness", "add": "#ch", "mode": "doorbell"}` | Add background channel |
| Awareness remove | `{"action": "awareness", "remove": "#ch"}` | Remove background channel |
| Awareness default | `{"action": "awareness", "default": "pending"}` | Change default mode |
| Awareness TTL | `{"action": "awareness", "doorbell_ttl": {"irc": 3}}` | Set doorbell expiry |
| Awareness attach | `{"action": "awareness", "attach": "arena"}` | Add adapter to direct_attach |
| Awareness detach | `{"action": "awareness", "detach": "arena"}` | Remove adapter from direct_attach |
| Reasoning effort | `{"action": "reasoning_effort", "level": "xhigh"}` | Change reasoning depth via session/set_model. Levels: low, medium, high, xhigh. Turn-limited (default 5), auto-reverts to configured default. No restart. |

## Compaction Instructions

When asdaaas sends `/compact` to the binary, it appends instructions telling the binary what to preserve in the compacted summary. Three layers of configuration (highest priority first):

1. **Per-request override:** `{"action": "compact", "instructions": "Just keep the last 3 entries."}` — overrides everything for this one compaction.
2. **Per-agent file:** `~/agents/<Name>/asdaaas/compaction_instructions.txt` — plain text file, read on every compact. Overrides the default.
3. **Default constant:** `DEFAULT_COMPACTION_INSTRUCTIONS` in asdaaas.py — preserves identity, corrections, pending work, file paths, recent commits, open issues, and active conversation context.

### Setting per-agent instructions

Create a text file at `~/agents/<Name>/asdaaas/compaction_instructions.txt`:

```
Preserve: my corrections log (all 7 entries), current test backlog table,
active feature requests, MockBinary test infrastructure state, and any
conversation context with Eric. Omit completed work older than 48 hours.
```

### Per-request override

Pass `"instructions"` in the compact command to override for a single compaction:

```python
cmd = {"action": "compact", "instructions": "Emergency compact: keep only last 5 notebook entries and pending issues."}
```

### Which paths use instructions

| Path | Triggered by | Uses instructions? |
|------|-------------|-------------------|
| Agent-initiated | `{"action": "compact"}` command | Yes — per-request > per-agent > default |
| Force compact | `{"action": "force_compact"}` command | Yes — per-agent > default (no per-request) |
| Auto-compaction | Binary hits 85% context | No — binary decides internally. Post-compaction orientation includes "Follow your boot protocol." |