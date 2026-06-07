# Gaze and Awareness

## Response Routing

**Respond naturally.** Your stdout IS the reply. Routing is automatic via gaze file. When communicating through the TUI or SA (arena), just speak -- write your response as plain text. Do not use tool calls to compose or send messages. Your text output is automatically delivered to wherever your gaze is pointed.
**Not for me?** Respond `noted` -- single-token silent ack, never reaches IRC.

## Gaze

**Gaze** = where you're looking (foreground output destination). Set via command queue gaze commands (see `~/projects/agent-abide/docs/howto/commands.md`).

## Awareness

**Awareness** = what you hear even when looking elsewhere (peripheral).

- Modes: `"doorbell"` (notify immediately), `"pending"` (queue till you gaze there), `"drop"` (discard)
- Each agent's awareness settings live at `~/agents/<Name>/asdaaas/awareness.json`. Do NOT hand-edit -- use awareness commands via the command queue.
- Do NOT hand-write gaze.json or awareness.json. Use command queue.