# Agent-Abide Refactor Plan: Y-Channel + Modularity

**Author:** Sr  
**Date:** 2026-07-23  
**Status:** DRAFT — awaiting Eric's review  
**Prerequisites:** Y_CHANNEL_LSP_LESSONS.md (Trip, 2026-07-07), observer spec, TurnEngine extraction (s1)

---

## Problem Statement

asdaaas has three interleaved problems:

1. **Split visibility.** The binary emits state on two channels (files + stdout). The observer only sees files. `_process_stdout` sees stdout but discards everything it doesn't handle. We maintain shadow state (`_start_kwargs`, `_model_id`) that drifts from reality.

2. **Monolithic main().** `asdaaas.py` main() is ~750 lines mixing directory setup, observer lifecycle, command dispatch, turn orchestration, compaction detection, reasoning effort countdown, and shutdown. Adding a feature means editing this one function.

3. **Scattered protocol handling.** JSON-RPC framing, gate responses, and session management are mixed together in `grok_backend.py`. The backend is simultaneously a transport layer, a protocol handler, and a session manager.

These are not three separate refactors. They share a root cause: **no clear ownership boundaries between transport, state, and orchestration.**

---

## What I Think We've Missed

### A. The observer is a separate process

The observer runs as a **sidecar subprocess** (`asyncio.create_subprocess_exec`). It communicates via an atomic JSON file on disk, polled by `read_observer_state()`. This means:

- **Option B from my earlier proposal (call `observer.process_stdout_notification()`) doesn't work as stated.** The observer isn't a Python object in the asdaaas process — it's a separate PID reading files. You can't call a method on it.
- To feed stdout notifications to the observer, we'd need either: (a) the observer also tails `stdout_log.jsonl`, (b) a shared memory / pipe / socket channel, or (c) bring the observer in-process.
- Option (a) is the simplest and most consistent with the observer's existing architecture (it already tails files).

**This is a significant constraint I glossed over.** The observer's out-of-process nature means the Y-channel can't just "call the observer" — it has to emit data to a place the observer can read.

### B. The superset question has a likely answer

Looking at the event types the observer knows (`known_types.json`) vs what stdout carries:

- **updates.jsonl** carries: `user_message_chunk`, `agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update`, `turn_completed`, `retry_state`, `doom_loop_detected`, `compaction_*`, content chunks, token counts in `_meta`.
- **stdout** carries: `sessions/changed` (model, effort, activity), `models/update` (available models), gates (`session/request_permission`, `_x.ai/exit_plan_mode`, `_x.ai/ask_user_question`), `available_commands_update` (skill catalog), JSON-RPC responses to our requests.

These are **complementary, not overlapping.** stdout is NOT a superset of updates.jsonl. The observer needs BOTH feeds to have complete state. This means we're not heading toward "drop file tailing" — we're heading toward "merge two streams."

### C. Gate handling creates a circular dependency

Gates (plan review, permissions, ask_user_question) require the handler to **write back to stdin**. Currently `_process_stdout` does this because it has access to `self._proc.stdin`. If we extract a StdioTransport module, it must own both pipes — which means the backend's `_send()` method routes through it too. This is fine architecturally but means StdioTransport isn't just a reader module; it's the entire binary interface.

### D. The TurnEngine on s1 is stale

The TurnEngine extraction on s1 predates reasoning_effort, sixel, the new command handlers, and several fixes. It was merged into prod main along with the ephact work, but the asdaaas.py main() on prod still has all the command handlers inline. The TurnEngine on prod is a thin wrapper — the real extraction work (moving command dispatch, reasoning effort countdown, etc. into it) hasn't been done.

### E. What "modularity" actually means here

Not microservices. Not 50 files. The natural fault lines are:

| Concern | Current location | Natural module |
|---------|-----------------|----------------|
| Binary connection (pipes, framing, logging) | `grok_backend._process_stdout`, `_send` | **StdioTransport** (class in grok_backend.py or own file) |
| Gate responses | `grok_backend._handle_*` methods | **GateHandler** (registered on transport) |
| Session management (start, resume, compact) | `grok_backend.start()`, `resume()`, `send_prompt()` | Stays in **GrokBackend** (thin wrapper over transport) |
| Binary state (IDLE/BUSY/STUCK + model/effort) | `binary_state_observer.py` (file-based) | **BinaryStateObserver** (gains stdout_log.jsonl tail) |
| Shadow state (reasoning effort, model_id) | `_start_kwargs` in backend + countdown in main() | **Eliminated** — observer is source of truth |
| Command dispatch | Inline in main() (~100 lines) | **CommandDispatcher** (dict of action→handler) |
| Turn orchestration | main() + TurnEngine | **TurnEngine** (absorbs remaining main() turn logic) |
| main() | 750 lines | **Orchestrator** — wire modules, run loop. Target: <100 lines of glue. |

---

## Proposed Sequence

### Phase 1: Observer gains stdout visibility (the critical gap)

**Goal:** Observer sees both channels. Shadow state can be eliminated.

1. Observer's `ObserverService` gains a second tailer: `StdoutLogTailer` tailing `stdout_log.jsonl`.
2. New event processing: `process_stdout_event(frame)` handles `sessions/changed` → update model, effort, activity fields. `models/update` → update available models.
3. Observer state file gains new fields: `model_id`, `reasoning_effort`, `activity`, `available_models`.
4. asdaaas reads these from observer state instead of shadow copies.
5. **Test:** Observer unit tests with synthetic stdout_log entries. Integration test: change reasoning effort, verify observer state file reflects it within 1 poll cycle.

**Risk:** stdout_log.jsonl is written by `_process_stdout` with flush-per-line. Observer tailing it introduces a second reader. No locking needed (append-only, one writer, readers chase), but we need the same truncation-detection logic as UpdatesJSONLTailer.

**Doesn't touch:** asdaaas.py main(), grok_backend.py, gate handling. Minimal blast radius.

### Phase 2: Command dispatch extraction

**Goal:** main() command handling becomes a dispatch table instead of 100 lines of if/elif.

1. Create `command_handlers.py` with handler functions: `handle_ack()`, `handle_compact()`, `handle_gaze()`, `handle_reasoning_effort()`, `handle_awareness()`, etc.
2. Each handler takes a context object (backend, agent_name, config, observer_state, etc.) and the command dict.
3. main() replaces the if/elif chain with: `handler = HANDLERS.get(action); if handler: await handler(ctx, cmd)`.
4. Reasoning effort countdown moves into the reasoning_effort handler (it currently straddles main() and the handler).

**Risk:** Low. Mechanical extraction. Each handler is independently testable.

### Phase 3: StdioTransport extraction

**Goal:** Clean interface between "binary communication" and "what we do with the data."

1. Extract `StdioTransport` class from GrokBackend. Owns `_proc.stdin`, `_proc.stdout`, the read loop, framing, logging.
2. Transport exposes: `send(msg)`, `register_handler(method, callback)`, `on_notification(callback)`.
3. Gate handlers register on the transport: `transport.register_handler("session/request_permission", gate_handler.handle_permission)`.
4. `_process_stdout` becomes `transport.run()` — the main read loop.
5. GrokBackend becomes a thin session-management layer over transport: `start()`, `resume()`, `send_prompt()`, `set_reasoning_effort()` — all delegating to `transport.send()`.

**Risk:** Medium. Restructures the core binary interface. Needs careful testing of gate handling, session startup sequence, and the `_wait_for_response` pattern (used during startup).

**Note:** `_wait_for_response` is a special case — it needs to receive specific JSON-RPC responses by ID during startup. After startup, `_process_stdout` owns everything. The transport needs to support both modes: "synchronous wait for response" (startup) and "dispatch to handlers" (running).

### Phase 4: TurnEngine completion

**Goal:** main() becomes pure orchestration glue.

1. Move remaining turn logic from main() into TurnEngine: compaction detection, reasoning effort countdown, delay handling.
2. TurnEngine uses observer state (from Phase 1) instead of shadow state.
3. main() becomes: setup → wire modules → `while not shutdown: engine.run_turn()`.

**Risk:** Medium. TurnEngine already exists but is thin. This is the real extraction.

---

## What This Does NOT Include

- **Separate Y-channel process.** Not needed. The Y-channel is `_process_stdout` writing to `stdout_log.jsonl` (already done) + observer tailing it (Phase 1). No new process.
- **Dropping updates.jsonl.** The two streams are complementary. Observer needs both.
- **Adapter refactoring.** Adapters (localmail, IRC, TUI, etc.) are already reasonably modular.
- **Config refactoring.** `asdaaas_config.py` is fine as-is.

---

## Open Questions

1. **Observer in-process vs out-of-process.** The observer is currently a sidecar subprocess. This made sense when it only tailed one file. With two files + richer state, should it move in-process? Pro: eliminates file-based IPC, enables direct method calls, simpler. Con: observer crash takes down asdaaas (currently isolated), less testable in isolation. **My lean: keep out-of-process for now.** The file-based interface is simple and the isolation is valuable.

2. **Reasoning effort source of truth.** Currently: asdaaas sets effort via `session/set_model`, updates `_start_kwargs`, counts down turns in main(). With observer as SoT: observer reads `sessions/changed` from stdout_log and reports effort level. But the **countdown** is asdaaas-level logic, not binary state. Observer should report *what the binary thinks the effort is*; asdaaas should own *when to change it*. These are separate concerns.

3. **Dev/prod branch strategy going forward.** We just merged s1 → prod. Do we want a single branch (main) for all work, or keep a dev branch for risky changes? Single branch is simpler if we have tests. We have tests (25 ephact, interjection timing, plan review). **My lean: single branch with tests as the safety net.**

---

## Estimated Effort

| Phase | Scope | Estimate |
|-------|-------|----------|
| 1. Observer stdout visibility | New tailer + event processing + state fields + tests | 1-2 sessions |
| 2. Command dispatch extraction | Mechanical refactor + tests | 1 session |
| 3. StdioTransport extraction | Core restructure + gate handler migration + tests | 2-3 sessions |
| 4. TurnEngine completion | Absorb remaining main() logic + tests | 1-2 sessions |

Total: 5-8 sessions, incremental, each phase independently deployable.
