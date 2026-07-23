# Agent-Abide Refactor Plan: Y-Channel + Modularity

**Author:** Sr  
**Date:** 2026-07-23 (v3)  
**Status:** APPROVED — implementation starting  
**Prerequisites:** Y_CHANNEL_LSP_LESSONS.md (Trip, 2026-07-07), observer spec, TurnEngine extraction (s1)

---

## Motivating Principle

Modularity is evolvability. The right granularity is determined by the system's rate and type of change. The error should be on the side of slightly over-modular: over-modular is a flat interface tax (bounded, constant), under-modular is cascade bugs (unbounded, variable). Biology demonstrates this — Hox genes, stem cell niches, organ interfaces are all "over the line" for any individual organism, but that over-modularity is what made the Cambrian explosion possible.

**Target:** each module is a concern you can test, understand, or replace without loading the rest of the system into your head. main() becomes a composition root — setup, wiring, loop, teardown.

---

## Module Architecture

| Module | Concern | Current location |
|--------|---------|-----------------|
| **GrokBackend** | Binary connection, pipes, gates, session management | `grok_backend.py` (stays, gains observer feed) |
| **BinaryStateObserver** | Single source of truth for all binary state (updates.jsonl + stdout notifications). In-process asyncio task. | `binary_state_observer.py` (refactored from sidecar to in-process) |
| **InputGatherer** | Poll all input sources: doorbells, commands, adapters, interjections. Returns unified input set. | Spread across main() + helper functions |
| **OutputRouter** | Gaze-based response delivery to adapters (TUI/IRC/arena). | Inline in main() (~80 lines) |
| **CommandDispatcher** | Dispatch table: action → handler. Each handler is `(ctx, cmd) → side effects`. Handlers don't know about each other. | if/elif chain in main() (~100 lines) |
| **Pacer** | Delay logic, continue doorbells, turn timing, `until_event` handling. | Mixed into turn loop |
| **CompactionManager** | Detection, instructions, request, post-compaction state reset. | Scattered across main(), command handlers, backend |
| **HealthWriter** | Health files, profiles, session registry, dashboard state. | Inline in main() |
| **TurnEngine** | Orchestrator: wires all modules, runs gather→deliver→route→dispatch→pace cycle. | Partially extracted (thin wrapper) |

### Target main()

```python
async def main(agent_name, ...):
    # Setup
    backend = GrokBackend(...)
    await backend.start(...)
    observer = BinaryStateObserver(pid=backend.proc.pid, ...)
    asyncio.create_task(observer.run())
    backend.set_observer(observer)

    # Wire modules
    gatherer = InputGatherer(agent_name, awareness, adapters)
    router = OutputRouter(agent_name)
    dispatcher = CommandDispatcher(backend, observer)
    pacer = Pacer(agent_name)
    compaction = CompactionManager(backend, observer, agent_name)
    health = HealthWriter(agent_name, observer)
    engine = TurnEngine(
        gatherer, router, dispatcher, pacer,
        compaction, health, backend, observer
    )

    # Run
    while not shutdown:
        await engine.run_turn()

    # Shutdown
    observer.stop()
    await backend.stop()
```

---

## Technical Clarifications

### Stdout is complementary, not a superset

| Stream | Carries |
|--------|---------|
| **updates.jsonl** (file) | Content chunks, tool calls, turn lifecycle, retry state, doom loop, compaction, token counts |
| **stdout** (pipe) | Model/effort/activity (`sessions/changed`), available models (`models/update`), gates, skill catalog, JSON-RPC responses |

Observer needs both feeds. We're merging two streams, not replacing one.

### No stdout pipe contention

`_process_stdout` is the exclusive pipe reader. The turn cycle reads from updates.jsonl (file). No race condition. The problem is conceptual: `_process_stdout` discards what the observer needs.

### Observer moves in-process

- File-based IPC adds 250ms+ per hop. In-process: same tick.
- Isolation benefit is near-zero (if asdaaas dies, nothing to observe).
- State machine (`BinaryStateObserver.process_event()`) is already pure and testable.
- Still writes state file for external consumers (dashboards, health checks) at reduced frequency.

### Gate handlers stay in backend

~100 lines, 3 gate types, fixed set. Not worth extracting. They stay inside GrokBackend.

---

## Implementation Sequence

### Phase 1: Observer in-process + stdout feed

1. Move `BinaryStateObserver` from sidecar subprocess to asyncio task.
2. Add `process_stdout_event(frame)` for `sessions/changed` and `models/update`.
3. `_process_stdout` calls observer after logging. ~5 lines.
4. Observer state gains: `model_id`, `reasoning_effort`, `activity`.
5. Kill shadow state (`_start_kwargs["reasoning_effort"]`, `_model_id`).
6. Observer `reset(new_pid)` for binary restarts.
7. State file still written for dashboards (every 1s, not 250ms).

**Tests:** Observer unit tests with synthetic stdout events. Integration: change effort, verify observer reflects it.

### Phase 2: CommandDispatcher + Pacer extraction

1. `command_handlers.py`: handler functions with `CommandContext`.
2. Dispatch table replaces if/elif chain.
3. Reasoning effort countdown moves entirely into handler.
4. `pacer.py`: delay logic, continue doorbells, `until_event`, timing.
5. `cmd_fix` (dual continue problem) becomes a Pacer concern, isolated and fixable.

**Tests:** Each handler with mock context. Pacer timing tests.

### Phase 3: InputGatherer + OutputRouter + HealthWriter

1. `InputGatherer`: encapsulates all polling. Returns structured result.
2. `OutputRouter`: encapsulates gaze-based delivery.
3. `HealthWriter`: encapsulates all observability output.
4. These are grouping existing code behind class interfaces.

**Tests:** Mock filesystem/adapters, verify gathering/routing/writing independently.

### Phase 4: CompactionManager + TurnEngine completion

1. `CompactionManager`: detection, instructions, request, post-compaction reset.
2. TurnEngine absorbs remaining main() loop logic.
3. main() reduces to composition root.

**Tests:** CompactionManager with mock backend/observer. TurnEngine integration tests.

---

## Open Questions

1. **Observer state file frequency.** 250ms is for the sidecar's heartbeat. In-process, we write for external consumers only. 1s? 2s? Driven by dashboard refresh rate.

2. **Observer across binary restarts.** `cancel_and_restart()` kills PID. Observer needs `reset(new_pid)`. The updates.jsonl path also changes (new session dir). Observer must re-orient.

3. **Branch strategy.** Single branch (main) with tests as safety net. Each phase is a PR-sized commit or small series.

---

## Estimated Effort

| Phase | Scope | Estimate |
|-------|-------|----------|
| 1. Observer in-process + stdout | Core state architecture | 2-3 sessions |
| 2. CommandDispatcher + Pacer | Command handling + timing | 1-2 sessions |
| 3. InputGatherer + OutputRouter + HealthWriter | Input/output/health | 1-2 sessions |
| 4. CompactionManager + TurnEngine | Final main() reduction | 1-2 sessions |

Total: 5-9 sessions, each phase independently deployable and testable.
