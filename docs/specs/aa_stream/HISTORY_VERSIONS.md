# METHOD — History versions we construct

*2026-09-10. Codify after continuity + USB audits. Eric: “codify what versions we want to construct.”*

## Do we have enough to decide?

**Yes.** Evidence in hand:

| Evidence | What it taught |
|----------|----------------|
| Live + local tar + USB + git session_backups | One-active UUID; true handoffs; early spines recoverable |
| `full_session_history/` Jun1 extract | Concrete mass for Sr/Jr/Q/Cinco/Trip chain steps |
| conv vs updates audit | Speech log ≠ full stream; conv incomplete historically |
| Model-change audit | Model lives on full stream (`modelId`), not conv |
| Dual-history lock | Lean speech + lifetime full stream + host meta; no standing UUID scanner |

We do **not** need more discovery to name the products. Further extract only **feeds** construction.

---

## Design axes (orthogonal)

Every “history” we build sits on these axes:

| Axis | Choices |
|------|---------|
| **Content density** | Speech-only · speech+tools · full backend stream |
| **Time span** | Single UUID · continuity chain (spines) · all UUIDs incl. sides · point-in-time backup freeze |
| **Authority** | Live binary SoR · agent-abide store · recovered archive |
| **Audience** | Model L1 / human read · forensic recreate · ops/debug · share/collab later |
| **Mutability** | Append-live · frozen snapshot · rebuilt migration |

---

## Versions we construct (canonical set)

### V1 — Lifetime speech log (lean)

| | |
|--|--|
| **Name** | `speech` / conversation |
| **Path (today)** | `{agent}/asdaaas/history/speech.jsonl (fallback: conversation.jsonl)` |
| **Job** | Navigational surface: what was **said** (user + agent) across the whole agent life |
| **Density** | Lean text turns; no tool payloads |
| **Span** | **Lifetime**, session-boundary robust — new UUID must **not** reset |
| **Meta (required going forward)** | `ts`, `role`, `session_id`, optional `msg_id` / `kind` |
| **Kinds KEEP** | `message` (incl. `[sent during your previous turn]` flag in content), `interjection`, `speech`, `thinking` |
| **Kinds DROP** | `doorbell` (ops: continue/clock/remind/…), `prompt` (assembled model input), `speech_repair` |
| **Not for** | Tool replay, model billing, full recreate, ops wakeups |
| **Status** | Live append; rich schema on Trip-G since 2026-09-11 13:14 restart; filter KEEP/DROP coded 13:25 (needs asdaaas restart to load) |

**Construct:** keep appending live; never fork on UUID change. Optional one-time repair from full stream where bubbles missing (later).

---

### V2 — Lifetime full stream (flight log)

| | |
|--|--|
| **Name** | `history` / updates-equivalent (was full_stream) |
| **Path (target)** | agent-abide store per agent (TBD path); **not** “only latest grok UUID” |
| **Job** | Complete recreate: tools, chunks, usage, `modelId`, errors |
| **Density** | Backend-normalized events + **host meta** |
| **Span** | **Lifetime** via continuity **chain** (spines in order); sides optional |
| **Host meta (minimum)** | `agent`, `cwd`/`home`, `family`, `backend`, `session_uuid`, `chain_index`, `role=spine\|side\|snapshot`, `source=live\|usb\|tar\|git`, `from`/`until` |
| **Join to V1** | `session_id` + time (and later msg anchors) |
| **Status** | Live mass split across UUID/USB/git; staging in `~/agents/full_session_history/` |

**Construct:**

1. **Migration (one-time):** ingest chain spines from live ∪ `full_session_history` ∪ tars ∪ git as needed → append-only agent-abide log.
2. **Ongoing:** tail live session UUID → append into same log.
3. **Sides:** ingest only if useful forensics; tag `role=side`; default readers skip.

---

### V3 — Continuity index (catalog, not a stream)

| | |
|--|--|
| **Name** | `continuity` |
| **Path (target)** | e.g. `{agent}/asdaaas/continuity.json` or agent-abide registry; audits already draft this |
| **Job** | Ordered truth of **which UUIDs** constitute the agent’s life, and where bytes live |
| **Contents** | `chain[]` (uuid, from, until, source, updates_mb, model family summary); `sides[]`; `gaps[]`; `live_uuid` |
| **Span** | Whole agent |
| **Status** | Audited in `AUDIT_agent_uuid_continuity_chains.md` + USB addenda; not yet a runtime file per agent |

**Construct:** derive from inventory + chain rules (one-active, contained=side); refresh when new UUID appears or archive ingested. **Not** a standing multi-agent scanner product — per-agent file updated on session start / migrate.

---

### V4 — Checkpoint snapshots (frozen)

| | |
|--|--|
| **Name** | `checkpoint` |
| **Examples** | USB lean Jun1 tree; Apr10 `*_session.tar.xz`; git `session_backups/*_DATE` |
| **Job** | Point-in-time freeze of a UUID (or whole home) for recovery / diff |
| **Density** | Whatever was on disk then (often full session dir) |
| **Span** | Instant (or backup window) |
| **Status** | Physical artifacts exist; not a first-class agent-abide API |

**Construct:** keep as **sources**, not as the navigational product. Index them from V3 (`source=usb:…`). Do not treat every checkpoint of the **same** UUID as a new chain step.

---

### V5 — Derived views (optional, later)

Built **from** V1+V2; not independent SoRs.

| View | From | Job |
|------|------|-----|
| **V5a Speech+attribution** | V1 + V2 modelId by time | “Who was speaking (model family) for this stretch?” |
| **V5b Tool timeline** | V2 only | Debug / eval of actions |
| **V5c Memory L1 pack** | V1 (+ notebook pointers) | Sticky subagent / plain-text recall |
| **V5d Share/collab slice** | V1 subset + doc versions | thiasai share MVP (different product surface) |

Ship V1–V3 first. V5 only when a consumer needs it.

---


---

## V2 ship ladder (Eric 2026-09-11 ~13:56 PDT)

Formalize V2 by climbing levels. **Do not skip to backend cleanup (L4).**

| Level | What | Consumer | Status |
|------:|------|----------|--------|
| **L1** | Searchable series of compressed full-stream chunks + manifest (`from_ts`/`until_ts`); hot tip unchanged (300→100, archive-before-prune) | ops / any reader that opens files | **DONE 2026-09-11** (lib+CLI+tests+smoke) |
| **L2** | Backend-agnostic stand-in **contract** for what frontends read (envelope, query API, non-goals) — paper first, then code types | shared | With L1 / just after |
| **L3** | Implement the stand-in **reader path in one frontend** | **Socratic Arena only** | After L1+L2 |
| **L4** | Clean up backends / session dirs / dual SoRs / stdout_log | binary + asdaaas | **Parked — scary; not required for L1–L3** |

### L3 scope (explicit)

| In | Out |
|----|-----|
| SA history pane + live tree read V2 stand-in (hot + archived chunks) | **AA-TUI** stays on live `updates.jsonl` for now |
| SA search / page / tail over lifetime stream where L1 exists | thiasai (later; take SA lessons) |
| Keep SA strengths (e.g. codeblock rendering); fix “never quite right” history | Forcing grok session layout changes |

**Rationale:** SA history has always been the weaker surface vs AA-TUI (except codeblocks). Getting SA right on a clean V2 contract is the proving ground; thiasai inherits lessons; AA-TUI cutover is optional later and must not block L1.

### Suggested build order

1. **L1 paths + seal tool** — **DONE 2026-09-11** (see below).
2. **L2 one-pager in this METHOD** — event envelope, `tail` / `range(t0,t1)` / `search`, SA adapter boundary.
3. **L3 SA** — `updates_parser` / `live_tailer` / `/api/agent/{name}/history*` grow a V2 source behind the same tree API; feature-flag if needed.
4. **Stop.** Document lessons. No L4 without a separate mandate.

### Hot format (aa.stream) — design locked 2026-09-11 ~14:50

Canonical spec + **version registry**: [`HOT_FORMAT_v1.md`](./HOT_FORMAT_v1.md) (agent-abide `docs/specs/aa_stream/`)

- Family `format: "aa.stream"`, integer `v` on every line + `hot.meta.json` + manifest rows
- Normalized spine/body for clients (SA first) + required `native` for backend fidelity
- Hot path: `{agent}/asdaaas/history/hot.jsonl` (not backend-native files)

### L1 layout & tools (locked)

**Path (per agent home):**
```
{agent_home}/asdaaas/history/
  manifest.jsonl              # append-only chunk index
  chunks/{chunk_id}.jsonl.zst # zstd level 3, updates-shaped JSONL
```
Hot tip remains live grok `…/sessions/<enc>/<uuid>/updates.jsonl` (not copied into this tree until sealed).

**Code:**
| | |
|--|--|
| Library | `core/full_stream.py` |
| CLI | `scripts/v2_full_stream.py` |
| Tests | `tests/test_full_stream_l1.py` |
| Specs | `docs/specs/aa_stream/` |

**CLI:**
```bash
python3 scripts/v2_full_stream.py --agent Trip-G init
python3 scripts/v2_full_stream.py --agent Trip-G seal --mib 50          # archive range, no hot rewrite
python3 scripts/v2_full_stream.py --agent Trip-G prune-hot              # dry-run
python3 scripts/v2_full_stream.py --agent Trip-G prune-hot --apply      # seal prefix + keep 100MiB tail
python3 scripts/v2_full_stream.py --agent Trip-G list
python3 scripts/v2_full_stream.py --agent Trip-G verify
python3 scripts/v2_full_stream.py --agent Trip-G cat-range --t0 EPOCH --t1 EPOCH
```

**Prune safety:** seal → zstd roundtrip verify (sha256) → only then `os.replace` hot tail. Default is dry-run. Do not `--apply` while racing a heavy writer without accepting a small race window.

**Manifest fields:** `chunk_id`, `path`, `from_ts`, `until_ts`, `*_iso`, `plain_bytes`, `zstd_bytes`, `line_count`, `sha256_plain`, `session_id`, `agent`, `source` (`seal`|`prune_hot`|…), `source_path`, `sealed_at`, `format=updates_jsonl_zstd_v1`.


## What we explicitly do **not** construct

| Non-version | Why |
|-------------|-----|
| Standing multi-agent UUID timeline daemon | Eric: one-off audit only |
| SA/TUI reading raw updates as speech SoR | Wrong density; use V1 |
| One blob that mixes all agents | Continuity is **per agent** |
| Resetting conv on new session | Breaks lifetime speech |
| Treating side UUID bursts as handoffs | Nested inside spine window |

---

## Per-agent construction recipe (operational)

```
for each agent in {Sr, Jr, Trip, Q, Cinco, Trip-G, Squiggy, …}:
  1. Load/build V3 continuity (chain + sides + gaps + sources)
  2. Ensure V1 speech file is lifetime append (restart asdaaas for schema)
  3. Migrate V2:
       for step in chain:
         pick best bytes (live if non-empty else full_session_history else tar else git)
         append into agent-abide full_stream with host meta
       optionally ingest sides tagged
  4. Wire live tail of current UUID → V2 append
  5. Leave V4 checkpoints indexed, not flattened as extra chain steps
```

**Priority mass already staged:** `~/agents/full_session_history/by_agent/` (Jun1 spines).

---

## Mapping: product questions → version

| Question | Answer with |
|----------|-------------|
| What did we say last month? | V1 |
| What tool ran before that answer? | V2 join from V1 |
| Which model was on? | V2 (`modelId`) / V5a |
| Did the agent change session UUID? | V3 |
| Can we restore Jr’s emptied first spine? | V4 USB/git → then into V2 |
| What should the sticky memory agent read? | V5c from V1 |

---


---

## Join path: V1 → V2 directly (ts)

*Eric 2026-09-10: one-active ⇒ no ts overlap (isn’t / mustn’t) across UUIDs for an agent ⇒ memory agent goes transcript → V2 chunk without a separate continuity hop.*

### Invariant (design)

For a given agent, **at most one spine UUID is active at a wall-clock time**. Therefore V2 lifetime stream (and its compressed chunks) can be indexed by **`ts` alone**:

```
V1 row.ts  →  chunk index [from_ts, until_ts)  →  decompress one .zst  →  filter events near ts
```

No mandatory V3 lookup at read time. V3 remains an **ops/catalog** artifact (where bytes came from, gaps, sides), not the runtime join key.

### Chunk index (sidecar, tiny)

Per agent, a small JSON/JSONL manifest:

```json
{"chunk": "v2/00042.jsonl.zst", "from_ts": "...", "until_ts": "...", "session_uuid": "...", "bytes_unz": 50000000}
```

Sorted by `from_ts`. Hundreds of rows — binary search is fine.

### Invariant is hard (Eric)

Timestamp ranges for an agent’s spine UUIDs **do not overlap and must not overlap** — “isn’t and shouldn’t ever be.”

V1 → V2 join is therefore **ts-only**. No runtime multi-chunk disambiguation path.

If an audit ever *appears* to show overlap (e.g. Jr 019d0a24 ∩ 019de4e1 on Jun1 extract, Sr ~2 min at handoff), that is a **data/ingest bug** to repair at migration time (trim, assign ownership, drop ghost), not a product feature. Do not encode clash heuristics into the memory agent.

### Compression (zstd)

Prefer **`.jsonl.zst`**. On this host (~100ms budget): **~100–200 MB plain / ~3–12 MB zstd** depending on content — much larger chunks than xz (~20 MB plain / ~1 MB xz).



---

## Hot full-detail file (TUI) vs archive (Eric 2026-09-11)

TUI history is **not V1**. V1 lacks tools/detail. TUI always reads a **full-detail** stream (live `updates.jsonl` or a local translation of it), **uncompressed** for speed.

Once V2 archive can recreate any period, the hot file is **pruned**; older mass lives only as compressed V2 chunks. Memory agent: V1 transcript → `ts` → archive chunk.

### Policy (locked — Eric 2026-09-11 ~10:55 PDT)

| | |
|--|--|
| **Grow until** | hot full-detail file reaches **300 MB** |
| **Prune back to** | **most recent 100 MB** (by file tail / time order) |
| **Before prune** | seal pruned prefix into V2 archive (`.jsonl.zst` chunks + manifest `from_ts`/`until_ts`) |
| **Never prune** | without successful archive write + index update |
| **Format** | uncompressed JSONL (updates-shaped or equivalent) |

**Hysteresis:** 300 → 100 avoids thrashing (**200 MB** released per cycle — same release mass as the earlier 250→50 sketch; higher floor keeps more interactive scrollback hot).

**Supersedes:** 250 → 50 (same morning, pre-rate-dist). Expanded floor after 5-day rate work + Q/Sr contamination context.

### Rough calendar meaning (order of magnitude)

Rates from spine `updates.jsonl` 5-day rolling dist (2026-09-11): Squiggy p50 ~8.5 MiB/day, p99 ~24; Trip-G p50 ~1.1.

| | Busy p50 (~8.5 MiB/day) | Busy p99 (~24 MiB/day) | Medium (~2.5 MiB/day) |
|--|------------------------:|-----------------------:|----------------------:|
| Hot floor 100 MB | ~12 d | ~4 d | ~5–6 weeks |
| Cycle 300→100 (200 MB) | ~24 d | ~8 d | ~2–3 months |

### Non-goals

- Using V1 as TUI scrollback SoR
- Keeping multi‑GB lifetime tips hot once archive is trusted
- Decompress on every TUI paint (archive is on-demand / deep history only)


## Open decisions (small, for Eric)

1. **V2 on-disk path** under agent-abide (single jsonl vs segmented by chain_index).
2. **Whether V1 backfill** from V2 is worth it for missing historical bubbles.
3. **Sides default:** omit from V2 default read path? (Recommend **yes**.)
4. **Trip-G / Squiggy:** V3 single spine + sides; V2 = live (+ sides optional); no USB early mass required.

---

## Related artifacts

| Doc / path | Role |
|------------|------|
| `METHOD_session_timeline_and_dual_history.md` | Original dual-layer lock (V1+V2) |
| `METHOD_history_versions.md` | **This file** — full version taxonomy |
| `AUDIT_agent_uuid_continuity_chains.md` | V3 draft data |
| `AUDIT_usb2_second_stick.md` / `AUDIT_backup_sources.md` | V4 sources |
| `~/agents/full_session_history/` | Staging bytes for V2 migrate |
| `AUDIT_conversation_vs_updates.md` | Why V1 ≠ V2 |
| `AUDIT_agent_model_changes.md` | Model on V2 |

## Done when (codify phase)

- [x] Versions named V1–V5 with jobs and non-goals  
- [ ] Eric agrees set (or edits)  
- [ ] V2 path + schema sketch in agent-abide  
- [ ] V3 file written per agent from audit  
- [ ] Migrate script: chain → V2 using `full_session_history` + live

## Layout rename (2026-09-14)

Canonical directory is `asdaaas/history/` (formerly `asdaaas/full_stream/`).

- Live code resolves `history/` first, then legacy `full_stream/` if present.
- V1 speech journal: `history/speech.jsonl`; root `conversation.jsonl` left intact and dual-written during cutover.
- SA aa_stream reads `history/hot.jsonl` with the same legacy fallback.

