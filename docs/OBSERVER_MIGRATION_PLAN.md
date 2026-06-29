# Binary State Observer — Migration Plan

**Author:** Sr (with Trip's scope assessment)
**Date:** 2026-06-29
**Status:** Approved for implementation

---

## What

Replace ~7 scattered state-inference heuristics in asdaaas.py with reads from the Binary State Observer's state file. The observer (committed fee1ee7, 59 tests passing) is a standalone sidecar process that tails `updates.jsonl` + monitors `/proc/[pid]/stat` and writes authoritative state to `~/agents/<Name>/asdaaas/binary_state.json` at 0.25s heartbeat with 1.0s TTL.

## Why

asdaaas currently infers binary state from multiple ad-hoc heuristics spread across ~10 locations. These race, conflict, and produce bugs (issues 0034, 0035, 0041). The observer centralizes state inference into one process with one output, making asdaaas a consumer of state rather than an inferrer of state.

## When

Starting 2026-06-29. Gate behind `observer_enabled` config flag. Remove old code paths after validation period.

---

## What Gets Replaced

| # | Current Heuristic | Location | Observer Replacement |
|---|-------------------|----------|---------------------|
| 1 | `backend.has_pending_tool_calls` | ~L2726 | `state == BUSY` |
| 2 | `consecutive_empty_doorbell` counter | ~L2930 | `doom_loop` flag in state |
| 3 | Keepalive/wall-clock timeout | scattered | `state == STUCK` + silence windows |
| 4 | `_is_midturn_message()` 30s grace | ~L630-666 | `state` + `since` timestamp |
| 5 | Token-drop compaction heuristic | main loop | Observer tracks `auto_compact_completed` |
| 6 | No retry awareness | — | `state == RETRYING` + attempt/reason |
| 7 | No process death monitoring | — | `state == GONE` + exit code |

## What Stays Unchanged

- `collect_response()` — content collection plumbing
- `_wait_for_receipt()` — delivery confirmation
- Doorbell/delay/continue logic — behavioral, not state inference
- Gaze/awareness — routing, not state
- All adapters — no changes needed

## Architecture

```
asdaaas.py                          binary_state_observer.py
    |                                       |
    |--- spawns after backend.start() ----->|
    |                                       |--- tails updates.jsonl
    |                                       |--- reads /proc/[pid]/stat
    |                                       |--- writes binary_state.json (0.25s)
    |<-- reads state file (atomic) ---------|
    |                                       |
    |--- reaps on shutdown ---------------->|
```

State file: `~/agents/<Name>/asdaaas/binary_state.json`
- Atomic write, 0.25s heartbeat, 1.0s TTL
- `read_state_file(path)` returns `None` if missing/corrupt/expired (dead observer)

## Implementation Sequence

### Phase 1: Scaffold (Sr)
1. Add `observer_enabled` flag to `asdaaas_config.py`
2. Add observer spawn after `backend.start()` in main loop
3. Add observer reap on shutdown
4. Add `read_state_file()` import and helper
5. Enhance `health.json` with observer state field

### Phase 2: Swap heuristics (Sr)
Replace each heuristic with observer read, gated by `observer_enabled`:
1. `has_pending_tool_calls` → BUSY check (prevents stale continues)
2. Midturn detection → state + since timestamp
3. Empty doorbell backoff → STUCK detection
4. Doom loop detection → doom_loop flag
5. 3s collection window → IDLE check
6. Add GONE handling (process death recovery)
7. Add RETRYING awareness (log, don't interfere)

### Phase 3: Tests (Trip)
- **10 new integration tests** (I1–I10 from Trip's assessment)
- **2 MockBinary changes** (M1: scenario event coverage, M2: MockObserverStateFile helper)
- **1 existing test to watch** (`test_no_stale_continues_during_long_tool_call` — passes with gate, needs update if old path removed)

### Phase 4: Validate
- Run all agents with `observer_enabled=true` for 24-48h
- Compare behavior against old heuristics
- Monitor for regressions

### Phase 5: Remove old paths
- Remove gated old code
- Update the 1 mock scenario test
- Delete `has_pending_tool_calls` from backend interface

## Config

```python
# asdaaas_config.py
observer_enabled: bool = True  # default True once validated
observer_state_file: str = "~/agents/{name}/asdaaas/binary_state.json"
```

## Risks

| Risk | Mitigation |
|------|-----------|
| Observer crashes mid-run | State file expires (1.0s TTL) → `read_state_file()` returns None → fallback to old heuristics |
| State file stale | TTL enforcement — expired = ignored |
| Observer lags behind binary | 0.25s heartbeat is 4x faster than asdaaas needs |
| Regression in decision-making | Config flag toggle, 24-48h validation period |

## Success Criteria

- All 295+ existing tests pass
- 10 new observer integration tests pass
- Agents run 48h with observer_enabled=true, no regressions
- Issues 0034, 0035, 0041 don't recur
- `has_pending_tool_calls`, `consecutive_empty_doorbell`, midturn grace window code removed