# S2 Refactor: Public Readiness

**Author:** Sr
**Date:** 2026-07-08
**Status:** Draft
**Branch:** TBD (will branch from main post-merge)
**Repo:** ~/projects/agent-abide-dev/
**Inputs:** Squiggy's modularity assessment, S1 outcomes, open issues 0004/0010/0011

## Motivation

Agent-abide is a leading item on Eric's resume and represents the MikeyV research program publicly. The architecture is sound — the code should reflect it. Someone reading the repo should see named modules with clear responsibilities, not a 2600-line engine file they have to take on faith.

## Goals

1. **Code matches architecture** — someone reading the repo sees gaze, doorbells, commands, delay as distinct concepts with distinct code
2. **Contributor-friendly** — a new adapter can be written by reading one file + one example, not by understanding the whole system
3. **Stable pacing** — the continue/wait/compact logic has explicit rules, not scattered conditionals
4. **Time-aware agents** — agents know "Eric spoke 47s ago" without burning a tool call
5. **Clean public docs** — the repo tells its own story

## Non-goals

- Performance optimization (folder watching, async polling) — later, measured
- New features — S2 is restructuring, not capability
- Replacing the filesystem model — that's the philosophy, not the problem

## Phase 1: Engine split (~5 modules extracted)

Split `asdaaas.py` (~2600 lines) along its natural seams. Each module gets a clear contract. `asdaaas.py` shrinks to orchestration only (main loop, startup, shutdown).

| New module | Functions moving | Lines (approx) | What it owns |
|------------|-----------------|-----------------|--------------|
| `core/gaze.py` | read_gaze, write_gaze, _build_gaze, gaze_label, get_room, get_msg_room, matches_gaze | ~120 | Where the agent is looking |
| `core/doorbells.py` | poll_doorbells, has_pending_doorbells, ack_doorbells, format_doorbell, queue_continue_doorbell, _cleanup_compact_doorbells, _cleanup_continue_doorbells, _queue_post_compaction_doorbell | ~220 | Persistent notifications |
| `core/commands.py` | poll_commands, has_pending_commands, write_command, CommandWatchdog, cancel_turn_flag_path, watch_cancel_flag | ~200 | Agent command queue |
| `core/awareness.py` | read_awareness, write_awareness, _apply_awareness_command, get_background_mode, format_background_doorbell, poll_adapter_inboxes, has_pending_adapter_messages | ~200 | What the agent hears peripherally |
| `core/delay.py` | run_delay_loop | ~50 | Pacing / sleep logic |
| `core/protocol.py` | rpc_request, rpc_notification, read_frame, send, wait_for_response, collect_response, drain_stale_frames | ~250 | JSON-RPC wire protocol to binary |
| `core/health.py` | write_health, write_profile, write_conversation, write_compaction_state, get_compaction_instructions, context_left_tag, MessageTimer | ~200 | Status files + context tracking |

**What stays in asdaaas.py:** main(), startup, shutdown, signal handling, the orchestration loop that calls these modules. Target: < 800 lines.

### Conversion pattern
- Extract functions to new module with same signatures
- Add `from gaze import read_gaze, matches_gaze` etc. to asdaaas.py
- Run full test suite
- Commit per module

### Safety rule
No behavior changes. Same functions, same signatures, same file formats. Just different addresses. Tests must pass after each extraction.

## Phase 2: Pacing rules

Write explicit state chart for the continue/wait/compact decision. Today this logic is scattered across run_delay_loop, queue_continue_doorbell, turn_engine drain logic, and main().

### Deliverables
- `docs/PACING_RULES.md` — the decision table in prose
- `core/pacing.py` — single `should_continue(state) -> Action` function
- Named test scenarios for every past pacing bug:
  - Delay ignored, turns stacked (Squiggy session 1)
  - Acked bell redelivered
  - Compaction request during active turn
  - Mid-turn message classification

### The decision table (draft)

| Agent said | Messages pending? | Current state | Action |
|-----------|-------------------|---------------|--------|
| delay 0 | — | — | immediate continue |
| delay N | — | — | sleep N, then continue |
| delay until_event | no | idle | no continue queued |
| delay until_event | yes | idle | continue (event arrived) |
| — (no command) | — | turn complete | continue (default) |
| compact | — | — | trigger compaction flow |

This gets refined during implementation, but the point is: one table, one function, tested.

## Phase 3: Adapter starter kit

Make writing a new adapter obvious.

### Deliverables
- `adapters/base_adapter.py` — base class or documented template with: registration, heartbeat, shutdown, agent-list discovery
- `docs/WRITING_AN_ADAPTER.md` — step-by-step guide with example
- Refactor one existing adapter (remind or heartbeat, smallest) to use the base

### What an adapter author needs to know
1. How to register (write JSON to adapters/)
2. How to discover agents (read config or running_agents)
3. How to deliver messages (write to agent inbox)
4. How to receive (poll outbox)
5. How to heartbeat (update registration)
6. How to shut down cleanly

## Phase 4: Time signals

Issue 0010. Give agents cheap temporal awareness without tool calls.

### Deliverables
- Continue doorbell text includes: `[last human input: 47s ago]` or `[last human input: 3h12m ago]`
- Doorbell text includes age: `[this message: 12s old]`
- Context tag includes time-of-day: `[14:32 PDT | ...]`

### Where this lives
- `core/doorbells.py` — format_doorbell adds age
- `core/asdaaas.py` main loop — continue text includes last-human-input delta
- `core/health.py` — context_left_tag adds wall clock

## Phase 5: Public docs

### Deliverables
- `README.md` rewrite for public audience: what it is, why it exists, how to install, how to extend
- `docs/ARCHITECTURE.md` updated to match new module structure
- `docs/ADAPTER_CATALOG.md` updated
- `CONTRIBUTING.md` — how to contribute (PR process, test requirements, module boundaries)
- License decision (Eric's call)

## Sequencing

| Phase | Depends on | Estimated scope |
|-------|-----------|-----------------|
| 1 (Engine split) | Nothing | Medium — mechanical extraction |
| 2 (Pacing rules) | Phase 1 (split makes the code addressable) | Medium — design + implementation |
| 3 (Adapter kit) | Phase 1 (need stable module boundaries) | Small |
| 4 (Time signals) | Phase 1 (doorbells.py exists) | Small |
| 5 (Public docs) | Phases 1-4 (document what exists) | Medium — writing |

Phases 3 and 4 can run in parallel. Phase 5 runs last because it documents the final state.

## Success criteria

| Goal | How we'll know |
|------|---------------|
| Code matches architecture | asdaaas.py < 800 lines; each concept has a named module |
| Contributor-friendly | New adapter writable from docs + base class in < 1 hour |
| Stable pacing | Zero pacing regressions; named test for each past bug |
| Time-aware agents | Agents stop calling `date` for orientation |
| Clean public docs | Someone can clone, understand, and run without asking us |

## What not to do (from Squiggy's writeup)

1. Don't replace the filesystem with a central router
2. Don't put importance filtering in the harness
3. Don't rebuild protocol and turn logic simultaneously
4. Don't turn MockBinary into a second real agent
5. Don't block useful splits on a perfect architecture paper
