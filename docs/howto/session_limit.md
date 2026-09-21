# Session / usage limit handler

## Goals (Eric)

1. **Know** it's happening (health, control line, logs)
2. **Park messages** for later (inbox/doorbells keep filling; no continue thrash)
3. **Wake** when the limit resets

## Flow

```
backend detects limit language / result error
  → stop_reason=session_limit
turn_engine handle_session_limit
  → asdaaas/session_limit.json park state
  → health status=session_limited
  → [aa.control] conversation line
  → delay continues until reset (or 1h default)
  → schedule_self_restart at reset if ≤2h
on restart start
  → clear park + control "back online"
```

## Detection

`core/session_limit.py` patterns: session/usage/rate limit, try again in N, resets at HH:MM.

Claude: result frame + assistant text. Grok can call the same inspect helpers later.

## Agent view

Health bar: `session_limited`. Conversation: control line with snippet + wake_in.

## Future: Claude cache cold telemetry

Claude Code may report when a model's prompt cache has gone cold. For
subscription-based agents we may want a **cache warmer** (periodic cheap
touch) so cold-start latency does not stack with session-limit wake.
Not implemented yet — track separately from session_limit park/wake.
