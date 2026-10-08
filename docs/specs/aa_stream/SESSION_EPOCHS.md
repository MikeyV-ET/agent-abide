# Session epochs — carry context across disposable backend sessions

**Status:** concept (Eric 2026-10-08). Not implemented.  
**Lock change:** we **abandon** “one UUID for the life of the agent.” Backend session ids are **epochs**. The agent (Squiggy, Trip-G, …) is the durable identity.

Related: [`HISTORY_VERSIONS.md`](./HISTORY_VERSIONS.md) (V1 speech, V2 aa.stream, V3 continuity), [`AUDIT_backend_session_schemas.md`](./AUDIT_backend_session_schemas.md), [`V1_TRANSCRIPT.md`](./V1_TRANSCRIPT.md), [`HOT_FORMAT_v1.md`](./HOT_FORMAT_v1.md).

---

## Why

Grok Squiggy `019f4394-…f44e` is ~3.4G / 115k messages. Cold `session/load` after reboot is ~4.5 min; a 120s wait used to kill a still-loading binary. Claude Code and Codex have the same shape: native JSONL journals that grow without bound, with **no shared file format**.

Loading last year’s native journal is not a continuity strategy. **Packing into a new session is.**

---

## Three planes (keep them separate)

| Plane | Durable? | Job |
|-------|----------|-----|
| **Agent** | Yes | Name, home, model family, V1 speech, V2 full stream, V3 continuity, notebook |
| **Epoch** | One live | The backend’s current session/thread id (grok uuid, Claude `--resume` id, Codex thread) |
| **Native journal** | Disposable | `updates.jsonl` / Claude `<sid>.jsonl` / Codex `rollout-*.jsonl` — cache for *this* epoch only |

V1 already says: *span is lifetime; new UUID must not reset speech.* V3 already names the UUID chain. What we never built is the **runtime carry**: on `session/new`, inject a pack so the model is not amnesiac.

---

## Carry (the missing runtime)

```
          V1 speech ──┐
          V2 hot/archive (optional slice) ─┼─► CarryPack ──► Backend.seed()
          notebook / METHOD pointers ─────┘         │
                                                    ▼
                         V3 continuity.json  {chain[], live_uuid, pack_id}
```

**CarryPack** (backend-agnostic bytes we already almost have):

- Token-budgeted V1 transcript (`v1_transcript.py --memory-pack` + a recency window)
- Optional last-N tool *summaries* from V2/hot (not raw payloads)
- Epoch pointer: previous uuid, why we cut (disk / load-time / session_limit / operator)
- Not: the 711M `updates.jsonl`

**When we mint a new epoch**

1. Seal V2 hot chunk for the dying epoch.
2. Append V3 `chain[]` row (`uuid`, `from`, `until`, `backend`, `reason`).
3. Backend `session/new` (or Claude without `--resume`, Codex new thread).
4. `Backend.seed(pack)` — first prompt / seed message, **asdaaas-owned**.
5. Record `live_uuid`. Do **not** require writing `agents.json` (asdaaas still must not mint into the roster). Live id lives in `{agent}/asdaaas/continuity.json`. `restart_agent.sh` reads that, with roster `session` as a stale hint only.

**Never silent.** Operator command, budget trip, or `session_limit` may request a cut. The cut is logged on V3. The model sees an explicit “epoch N+1, here is what you were in the middle of.”

---

## Backend adapters (one interface, native inject)

`AgentBackend` already has `start(..., session_id=None) -> session_id`. Add:

| Method | Meaning |
|--------|---------|
| `start(session_id=None)` | `None` = new epoch. Non-None = resume *this* epoch if cheap. |
| `seed(pack: str)` | Inject carry. Must not look like Eric-in-the-room if we can help it (system / `aa.control` / first synthetic user, painted as system in TUI). |
| `native_weight() -> bytes` | Disk of the live native journal, for budgets. |

| Backend | Resume | New epoch | Seed |
|---------|--------|-----------|------|
| **Grok** | `session/load` | `session/new` | `session/prompt` with pack (or a dedicated notification if the binary grows one). Load is **optional** and budgeted; over budget → new+seed, do not wait 10 min. |
| **Claude Code** | `--resume <sid>` | omit resume (new jsonl) | first user message on stdin; files (`CLAUDE.md`) are *not* conversation |
| **Codex** | existing thread id | new thread / new rollout file | first turn on the thread |
| **Next** | whatever that binary’s resume handle is | mint | first-turn inject |

Native files stay **sources** for V2 mappers (already the L1/L2 contract). They are not the continuity SoR.

---

## Budgets (trip a cut)

| Meter | Warn | Cut |
|-------|------|-----|
| Native journal disk | 1 GiB | 2 GiB |
| Cold load time | 60 s | 120 s (do not sit in `session/load`) |
| API tokens | compaction threshold (already 0.8) | backend `session_limit` |

A cut is cheaper than a 3.4G load. Y-channel (`stdout_log` raw prefix) can still be archived inside an epoch; that is hygiene, not continuity.

---

## What we stop doing

- Treating roster `agents.json` `session` as a forever spine.
- `session/load` of a journal that misses the load-time budget.
- Hoping the binary will remember last week because the uuid is old.

## What we keep

- V1 lifetime speech across epochs.
- V2 aa.stream lifetime full stream (hot + sealed chunks).
- V3 continuity chain as the catalog of epochs.
- No asdaaas writes of minted ids into `agents.json`.

---

## First implementation slice (when building, not today)

1. `{agent}/asdaaas/continuity.json` + restart_agent reads `live_uuid`.
2. `CarryPack` from existing `v1_transcript.py` (token cap).
3. Grok `session/new` + `seed` painted as system in TUI.
4. Claude + Codex seed adapters second (same pack, different inject).
