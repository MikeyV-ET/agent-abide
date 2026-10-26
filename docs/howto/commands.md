# Command Queue Reference

Write JSON command files to `~/agents/<Name>/asdaaas/commands/cmd_{timestamp}_{rand}.json`. Multiple commands per turn supported.

## Commands

| Command | Format | Purpose |
|---------|--------|---------|
| Delay | `{"action": "delay", "seconds": N}` | Pause N seconds before next turn. `0`=immediate, `"until_event"`=sleep till event. Optional `"text"` field replaces the default continue message (one-shot, consumed after delivery). Bare `delay 0` with no `text` gets a short how-to on the next continue (how to park, wait N, or leave a note to yourself) so a confused agent is not left spinning. |
| Ack | `{"action": "ack", "handled": ["id1", "id2"]}` | Clear handled doorbells |
| Flag | `{"action": "flag", "status": "waiting"}` | Orange tab: waiting on Eric. `"error"` = red. `"clear"` = auto (green working / gray idle). Optional `"note"`. |
| Piggyback ack | `{"action": "delay", ..., "ack": ["id1"]}` | Combine delay + ack atomically |
| Compact | `{"action": "compact"}` | Request self-compaction (executes immediately). On **grok agent stdio**, extra `"instructions"` are **ignored** (binary `/compact` takes no argument). Other backends may still append preserve-text. |
| Ensure host service | `{"action": "ensure_service", "service": "remind"\|"localmail"}` | Start/status/stop host adapters via asdaaas (not model bash). See host_services.md. |
| Restart (self) | `{"action": "restart"}` | Schedule `restart_agent.sh --force <self>` then graceful shutdown. Optional `"reason"` / `"text"`, `"delay_s"` (default 2). Process exits; script brings agent back on **this** tree (dev or prod). |
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
| Reasoning effort | `{"action": "reasoning_effort", "level": "high"}` | Change reasoning depth via session/set_model. Levels: low, medium, high. Turn-limited (default 5), auto-reverts to configured default. No restart. |

## Compaction Instructions

**Grok agent stdio:** `/compact` takes no extra text. asdaaas sends a bare `/compact`. `"instructions"` on the command, `compaction_instructions.txt`, and `DEFAULT_COMPACTION_INSTRUCTIONS` are **not** passed to the summarizer. After compact: AGENTS.md re-injected, boot protocol (notes + lab tail). Auto-compaction never used those instructions.

**Other backends** (if they still accept `/compact <context>`): asdaaas appends preserve-text. Three layers (highest priority first):

1. **Per-request override:** `{"action": "compact", "instructions": "Just keep the last 3 entries."}`
2. **Per-agent file:** `~/agents/<Name>/asdaaas/compaction_instructions.txt`
3. **Default constant:** `DEFAULT_COMPACTION_INSTRUCTIONS` in asdaaas.py

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

Ignored on grok agent stdio.

### Which paths use instructions

| Path | Triggered by | Uses instructions? |
|------|-------------|-------------------|
| Agent-initiated (grok) | `{"action": "compact"}` | **No** — bare `/compact` |
| Force compact (grok) | `{"action": "force_compact"}` | **No** — bare `/compact` |
| Agent-initiated (other backends) | `{"action": "compact"}` | Yes — per-request > per-agent > default |
| Auto-compaction | Binary hits 85% context | No — binary decides internally. Post-compaction orientation includes "Follow your boot protocol." |