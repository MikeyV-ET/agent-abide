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