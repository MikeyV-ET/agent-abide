# Hot spine (native → hot.jsonl)

## Law

`hot.jsonl` is the **AA unification layer** and the basis for a **unified memory
system** across backends — not only a TUI projection.

Native logs (Grok `updates.jsonl`, Claude session jsonl, …) stay the binary
flight recorder. Stream adapters map native → aa.stream. **hot** is the AA live tape.

## Single always-on ear (phase 1)

```
native file
    │
    ▼
HotSpine
    ├─ live: inotify + debounced sync (UpdatesHotWatcher)
    ├─ reconcile: periodic offset vs EOF → same sync_fn catch-up
    └─ projector: tail_*_once → append_hot_events (one append law)
```

Turn collect (`FileEventSource`) remains a **separate** reader for delivery.
Phase 2 may put collect + occupancy on a shared NativeTail bus; hot must not
wait for that.

## Reconcile

`behind = native_size - hot.meta checkpoint offset`

- Startup / session ready: catch-up until behind==0 (or no progress)
- Idle loop (~2s): measure + catch-up
- End of turn: `reconcile(catch_up=True)` via `_final_hot_sync`
- Before memory pack (callers): gate on `behind==0`

## Code

| Piece | Path |
|-------|------|
| Spine | `core/hot_spine.py` |
| Watch doorbell | `core/updates_hot_watch.py` |
| Grok projector | `core/stream_adapters/grok.py` `tail_grok_once` |
| Arm | `start_hot_spine_for_backend` after session_dir set |

## Ops

Log line on arm: `[hot_spine] armed agent=… native=… watcher=True reconcile=True`

If glass is stuck mid-tool and native has the tail, check `HotSpine.status()` /
`behind` before blaming paint.

## Phase 2 — shared native bus

```
updates.jsonl ── JsonlByteTail (one cursor)
       │
       ▼
 GrokNativeBus.pump()
       ├─► ingest_grok_records → hot.jsonl   (AA tape)
       └─► collect buffer → FileEventSource  (turn delivery)
events.jsonl ── second cursor (lifecycle only)
```

- Arm: session ready creates `GrokNativeBus` at hot checkpoint, `catch_up()`, then collect window at tip.
- `sync_hot_stream` / spine reconcile call `bus.pump()` (same ear).
- Occupancy still has its own tail (**phase 3**).
- Code: `core/native_tail.py`, `core/grok_native_bus.py`, `ingest_grok_records`.
