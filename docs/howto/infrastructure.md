# Infrastructure Reference

You run on `grok agent stdio` managed by asdaaas. Between your turns, you persist. You control where your voice goes (gaze), what reaches you (awareness), and when your next turn happens (delay). Everything is done by writing commands to your command queue.

## Systems

| System | Path | Purpose | Modify when |
|--------|------|---------|-------------|
| **Agent state** | `~/agents/` | Agent home dirs, AGENTS.md, PRINCIPLES.md, notebooks, doorbells, commands, issues/ | Always safe. This is agent state, not infrastructure code. |
| **Agent-abide** | `~/projects/agent-abide/` | asdaaas, adapters, TUI, API — single repo, trunk-based | Commit to main. Branch for risky work. **Don't merge + restart agents without Eric go-ahead.** |
| **Socratic Arena (prod)** | `~/projects/socratic-arena/` | SA frontend + backend, ports 8000/5173 | Pull from main when Eric says. |
| **Socratic Arena (dev)** | `~/projects/socratic-arena-dev/` | SA dev clone, ports 8002/5175 | Develop here, push to main. |

**Rules:**
- **agent-abide** is one repo. Develop on a branch, merge to main, restart agents with Eric's go-ahead.
- **Socratic Arena** uses two clones of one repo (dev + prod), both on main.
- **Tests** live in their respective repos.

**Config files (authoritative):**
- `~/projects/agent-abide/agents.json` — agent roster, backends, ports
- `~/agents/<Name>/asdaaas/awareness.json` — per-agent awareness (use command queue, don't hand-edit)

## Context Awareness

- `[Context left Xk]` tag at end of every prompt = primary awareness
- Health file: `~/agents/<Name>/asdaaas/health.json` (totalTokens, contextWindow)
- Auto-compaction at ~85%. Manage it yourself before that.
- After compaction, first health read may be stale. Re-read.

## When Stuck

1. Send localmail to Sr or Jr asking for help
2. Post in #standup on IRC
3. If all channels down, write to lab notebook and wait for heartbeat

## Deprecated -- Do Not Use

- `adapter_api.write_message()` -- old hub inbox, nothing reads it
- `leader_callback_client.py` -- deprecated