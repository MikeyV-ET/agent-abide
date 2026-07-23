# Agent-Abide Refactor Plan: Y-Channel + Modularity

**Author:** Sr  
**Date:** 2026-07-23 (revised)  
**Status:** DRAFT — awaiting Eric's review  
**Prerequisites:** Y_CHANNEL_LSP_LESSONS.md (Trip, 2026-07-07), observer spec, TurnEngine extraction (s1)

---

## Problem Statement

asdaaas has three interleaved problems that share a root cause — **no clear ownership boundaries between transport, state, and orchestration:**

1. **Split visibility.** The binary emits state on two channels (files + stdout). The observer only sees files. `_process_stdout` sees stdout but discards everything except gates. Shadow state (`_start_kwargs`, `_model_id`) drifts from reality.

2. **Monolithic main().** ~750 lines mixing setup, observer lifecycle, command dispatch, turn orchestration, compaction detection, reasoning effort countdown, and shutdown.

3. **No testable units.** You can't test command dispatch, output routing, or input gathering without bootstrapping the entire binary.

---

## Architectural Clarifications

### Stdout is NOT a superset of updates.jsonl

The two streams are complementary:

| Stream | Carries |
|--------|---------|
| **updates.jsonl** (file) | Content chunks, tool calls, turn lifecycle, retry state, doom loop, compaction events, token counts |
| **stdout** (pipe) | Model/effort/activity changes (`sessions/changed`), available models (`models/update`), gates, skill catalog, JSON-RPC responses |

The observer needs **both feeds** to have complete state. We're merging two streams, not replacing one.

### There is no stdout pipe contention

`_process_stdout` is the exclusive pipe reader. The turn cycle reads responses from **updates.jsonl** (the file), not from the stdout pipe. The race condition with `_wait_for_response` was already fixed (fire-and-forget for set_reasoning_effort). The problem Eric identified is conceptual: `_process_stdout` discards what the observer needs, and the observer can't see it.

### Observer should move in-process

The observer currently runs as a sidecar subprocess communicating via atomic JSON file (250ms polling). This was appropriate when it only tailed one file. With two data sources and a role as single source of truth, in-process is better:

- **Latency:** File-based IPC adds 250ms+ per hop (two hops for stdout → observer → asdaaas). In-process: same tick.
- **Simplicity:** Direct method calls replace file read/parse/write cycles.
- **Isolation cost is near-zero:** If asdaaas dies, there's nothing to observe. Observer is in asdaaas, not the binary — survives binary restarts.
- **The state machine is already pure:** `BinaryStateObserver.process_event()` is a testable function. `ObserverService` (file-tailing wrapper) is the part we're replacing.

### Gate handler extraction is premature

Gate handlers are ~100 lines, handle 3 gate types, and are unlikely to grow (the binary has a fixed gate set). Extracting them into a formal transport/handler registration system adds indirection for minimal benefit. They stay inside the backend.

---

## What main() Looks Like Today

```
main():
    SETUP          — directories, config, signals                   (~100 lines)
    START BINARY   — launch process, get session                    (~50 lines)  
    START OBSERVER — launch sidecar, read_observer_state()          (~30 lines)
    INIT STATE     — awareness, adapters, watchdog, pending queue   (~50 lines)
    
    LOOP:
        GATHER     — poll doorbells, commands, adapters, interjections
        DELIVER    — format prompt, send to binary, collect response
        ROUTE      — send response to gaze target (TUI/IRC/arena)
        DISPATCH   — process commands (delay/gaze/awareness/effort/compact)
        PACE       — delay logic, continue doorbells, timing
    
    SHUTDOWN       — reap observer, kill binary, unregister         (~50 lines)
```

Every verb in that loop is a distinct concern mixed into one function.

---

## Module Boundaries

**Principle:** A module is a concern you can test, understand, or replace without loading the rest of the system into your head.

| Module | Responsibility | Test strategy |
|--------|---------------|---------------|
| **InputGatherer** | Poll all input sources (doorbells, commands, adapters, interjections). Return unified input set. Doesn't know about the binary or output. | Mock filesystem, verify gathered messages |
| **OutputRouter** | Given a response and gaze state, deliver to the right adapter (TUI/IRC/arena). Doesn't know how responses are generated. | Mock adapters, verify routing decisions |
| **CommandDispatcher** | Dispatch table: action → handler function. Each handler is `(context, command) → side effects`. Handlers don't know about each other. | Mock context, verify each handler independently |
| **BinaryStateObserver** | Single source of truth for binary state. In-process asyncio task. Consumes updates.jsonl events + stdout notifications. Exposes state via attributes. | Existing unit tests + new stdout notification tests |
| **TurnEngine** | Orchestrates: gather → deliver → route → dispatch → pace. Wires modules together per-turn. | Integration tests with mock modules |
| **GrokBackend** | Binary connection: start, send, collect response, gates. Owns both pipes. Feeds stdout notifications to observer. | Existing MockBinary tests |

**What stays in main():**
- One-time setup (directories, config, signals) — sequential, not reusable
- Module construction and wiring — the composition root
- The `while not shutdown: engine.run_turn()` loop
- Teardown

**Target main():**
```python
async def main(agent_name, ...):
    # Setup
    backend = GrokBackend(...)
    await backend.start(...)
    observer = BinaryStateObserver(pid=backend.proc.pid, ...)
    asyncio.create_task(observer.run())  # tails updates.jsonl, receives stdout events
    backend.set_observer(observer)       # _process_stdout feeds notifications to observer
    
    # Wire modules
    gatherer = InputGatherer(agent_name, awareness, adapters)
    router = OutputRouter(agent_name)
    dispatcher = CommandDispatcher(backend, observer)
    engine = TurnEngine(gatherer, router, dispatcher, backend, observer)
    
    # Run
    while not shutdown:
        await engine.run_turn()
    
    # Shutdown
    observer.stop()
    await backend.stop()
```

~50 lines of glue. Every piece of logic lives in a module testable with mocks.

---

## Implementation Sequence

### Phase 1: Observer in-process + stdout feed

**Goal:** Observer becomes single source of truth for all binary state.

1. Move `BinaryStateObserver` from sidecar subprocess to asyncio task in asdaaas.
   - Keep the state machine class as-is (well-tested).
   - Replace `ObserverService` with an async task: tail updates.jsonl + expose state via attributes.
   - Still write state file for dashboards/health checks (but asdaaas reads directly).
2. Add `process_stdout_event(frame)` method to observer for `sessions/changed` and `models/update`.
3. `_process_stdout` calls `observer.process_stdout_event(frame)` after logging, before discarding. ~5 lines.
4. Observer state gains: `model_id`, `reasoning_effort`, `activity`.
5. Replace `_start_kwargs["reasoning_effort"]` and `_model_id` reads with observer reads.

**Tests:** Observer unit tests with synthetic stdout events. Integration: change effort via `session/set_model`, verify observer reflects it same tick.

**Blast radius:** observer launch in main(), `read_observer_state()` calls, shadow state in backend. Backend's `_process_stdout` gains one method call.

### Phase 2: Command dispatch extraction

**Goal:** Command handling becomes testable in isolation.

1. Create `command_handlers.py` with handler functions.
2. Each handler: `async def handle_X(ctx: CommandContext, cmd: dict) -> None`.
3. `CommandContext` bundles: backend, observer, agent_name, config.
4. Dispatch table: `HANDLERS = {"delay": handle_delay, "gaze": handle_gaze, ...}`.
5. Reasoning effort countdown moves entirely into the handler (currently split across handler + post-turn).
6. main() replaces if/elif chain with: `handler = HANDLERS.get(action); await handler(ctx, cmd)`.

**Tests:** Each handler testable with mock context. Countdown logic tested independently.

### Phase 3: InputGatherer + OutputRouter extraction

**Goal:** Input collection and output routing become testable units.

1. `InputGatherer`: encapsulates doorbell polling, command polling, adapter inbox reading, interjection checking. Returns a structured result.
2. `OutputRouter`: encapsulates gaze-based response delivery to adapters.
3. These are currently helper functions + inline code in main(). Extraction is mostly grouping existing code behind a class interface.

### Phase 4: TurnEngine completion

**Goal:** main() becomes pure glue.

1. TurnEngine wires together: gatherer.poll() → deliver to binary → router.route() → dispatcher.handle() → pacer.wait().
2. Absorbs remaining main() loop logic: compaction detection, health writes, timing.
3. main() reduces to: setup → wire → loop → teardown.

---

## What This Does NOT Include

- **Separate Y-channel process or module.** The Y-channel is `_process_stdout` feeding the observer. No new process, no new module.
- **StdioTransport extraction.** Gate handlers stay in backend. The backend IS the transport.
- **Dropping updates.jsonl.** Observer needs both streams (complementary, not overlapping).
- **Adapter refactoring.** Adapters are already modular.

---

## Open Questions

1. **State file for dashboards.** Observer moves in-process but dashboards/health checks still need the JSON file. Observer should still write it, but at a lower frequency (every 1s instead of 250ms). asdaaas reads state directly; file is for external consumers only.

2. **Observer across binary restarts.** `cancel_and_restart()` kills the binary PID. In-process observer detects GONE (PID died), then asdaaas restarts binary with new PID. Observer needs a `reset(new_pid)` method. Straightforward.

3. **Branch strategy.** Single branch (main) with tests as safety net. The s1 branch served its purpose; future work goes directly to main.

---

## Estimated Effort

| Phase | Scope | Estimate |
|-------|-------|----------|
| 1. Observer in-process + stdout feed | Core state architecture | 2-3 sessions |
| 2. Command dispatch extraction | Mechanical refactor | 1 session |
| 3. InputGatherer + OutputRouter | Grouping existing code | 1-2 sessions |
| 4. TurnEngine completion | Final main() reduction | 1-2 sessions |

Total: 5-8 sessions, each phase independently deployable and testable.
