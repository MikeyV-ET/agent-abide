# AA Stream Hot Format (aa.stream)

**Status:** design locked for implementation (Eric 2026-09-11 ~14:50 PDT)  
**Scope:** AA-owned hot tip + sealed chunks for multi-backend full stream.  
**Consumers (first):** Socratic Arena (L3).  
**Non-goals:** Do not rewrite backend stores; do not cut over AA-TUI yet.

Related (same directory): [`HISTORY_VERSIONS.md`](./HISTORY_VERSIONS.md), [`AUDIT_backend_session_schemas.md`](./AUDIT_backend_session_schemas.md).

**Canonical location:** `docs/specs/aa_stream/` in the agent-abide repo (travels with the project).

---

## 1. Do we have enough to design?

**Yes — for format v1.** Local samples + published layout docs cover:

| Backend | On-disk full stream | Sample in hand | Gaps acceptable for v1 |
|---------|---------------------|----------------|-------------------------|
| Grok | `updates.jsonl` | Trip-G live spine | Some rare sessionUpdate kinds → `unknown` |
| Codex | `rollout-*.jsonl` | Eric chat 2026-09-11 | app-server wire ≠ file; map file first |
| Claude | `projects/…/<sid>.jsonl` | testagent sessions | subagents/spills via ref + extra tailers |

v1 does **not** need perfect mappers for every exotic event. It needs a stable envelope that:
1. Clients can consume via normalized fields
2. Retains full native event when helpful
3. Extends to new backends without format break

Mapper completeness can grow under the same `v` until a **breaking** change forces `v: 2`.

---

## 2. Where things live

```
{agent_home}/asdaaas/history/
  FORMAT                    # optional pointer: "aa.stream/1" (human; meta is authoritative)
  hot.jsonl                 # hot tip (policy: grow 300 MiB → prune to 100 MiB)
  hot.meta.json             # tip metadata (version record, offsets, sessions)
  manifest.jsonl            # sealed chunk index (L1)
  chunks/{chunk_id}.jsonl.zst
  sources/{backend}.json    # tailer checkpoints (optional but recommended)
  schemas/                  # optional frozen dialect JSON Schemas
    aa.stream.v1.json
    …
```

**Version record is not only on each line** — also in `hot.meta.json` and each manifest chunk row so an empty or mid-migration tip is still identifiable.

---

## 3. Format version registry

| `format` id | `v` | Status | Introduced | Notes |
|-------------|-----|--------|------------|-------|
| `aa.stream` | **1** | **current** | 2026-09-11 | Spine + body + native; see §4–6 |
| `aa.stream` | 2 | reserved | — | Breaking spine/body changes only |

**Rules:**
- **`v`** is a positive integer on every event line and in meta/manifest.
- **Additive** fields (new optional body kinds, new backend enum values) stay on the same `v`.
- **Breaking** changes (rename/remove required spine fields, change `ts` units, redefine `class` meaning) → bump `v`, keep readers able to parse old chunks via `v` switch.
- Chunk files are immutable: each chunk records the `v` of lines it contains (single-v per chunk preferred; if mixed, manifest `v_min`/`v_max`).

---

## 4. Event line (`hot.jsonl` and decompressed chunks)

One JSON object per line. **Encoding:** UTF-8 JSONL, no pretty-print.

### 4.1 Required spine

| Field | Type | Meaning |
|-------|------|---------|
| `v` | int | Format version (**1**) |
| `format` | string | `"aa.stream"` (stable family id) |
| `ts` | number | Event time, **unix seconds** (float ok; ms inputs ÷1000) |
| `ts_iso` | string | UTC ISO-8601 (redundant, human/debug) |
| `agent` | string | AA agent name |
| `backend` | string | `grok` \| `codex` \| `claude` \| `asdaaas` \| future |
| `session_id` | string | Backend session/thread id (best effort; may be `"unknown"`) |
| `stream_seq` | int | Monotonic sequence **in this AA stream** (hot+chunks logical order) |
| `class` | string | Coarse kind: `message` \| `tool` \| `thought` \| `meta` \| `usage` \| `snapshot` \| `unknown` |
| `phase` | string | `start` \| `delta` \| `end` \| `full` \| `none` |
| `native` | object | Lossless backend event — **required in v1** when source had an event |

### 4.2 Optional spine

| Field | Type | Meaning |
|-------|------|---------|
| `role` | string | `user` \| `assistant` \| `system` \| `tool` \| `developer` \| `none` |
| `ids` | object | `{ "event"?, "parent"?, "turn"?, "item"? }` strings |
| `source` | object | `{ "path", "offset"?, "ordinal"?, "subagent"? }` for resync/dedupe |
| `body` | object | Normalized client projection (§5); omit only if truly unmapped |
| `body_map_v` | int | Mapper rules version (independent of format `v`) |
| `ts_source` | string | `backend` \| `ingest` \| `repair` |

### 4.3 `native` object

```json
"native": {
  "schema": "grok.session_update.v1",
  "event": { }
}
```

| `native.schema` (v1 set) | Source line |
|--------------------------|-------------|
| `grok.session_update.v1` | grok `updates.jsonl` object |
| `codex.rollout.v1` | codex rollout line `{timestamp, ordinal, type, payload}` |
| `claude.transcript.v1` | claude project session line |
| `asdaaas.host.v1` | optional host-injected events |

`event` is the **parsed native JSON object** (not a re-serialized string). Round-trip: serialize `event` back to a backend JSONL line.

### 4.4 Minimal valid example

```json
{"v":1,"format":"aa.stream","ts":1789161042.409,"ts_iso":"2026-09-11T21:10:42.409Z","agent":"Trip-G","backend":"codex","session_id":"01a0924e-…","stream_seq":9,"class":"message","phase":"full","role":"user","body":{"kind":"text","text":"hi"},"native":{"schema":"codex.rollout.v1","event":{}}}
```

---

## 5. Normalized `body` (client consumption)

`body.kind` is a closed set for v1; unknown native traffic uses `"kind":"raw_only"` (or omit body).

| `kind` | Main fields | Use |
|--------|-------------|-----|
| `text` | `text` | Complete user/agent text unit |
| `text_delta` | `text` | Streaming chunk |
| `thinking` | `text` | Complete thought (plaintext only) |
| `thinking_delta` | `text` | Streaming thought |
| `tool_call` | `id`, `name`, `args`?, `status`? | Tool invocation |
| `tool_result` | `tool_id`, `content`?, `ref`?, `status`? | Result; large → `ref` |
| `usage` | `input`?, `output`?, `cached`?, `total`? | Token accounting |
| `meta` | `label`?, `data`? | Turn edges, modes, non-chat |
| `snapshot` | `label`?, `ref`? | world_state / file-history style |
| `raw_only` | — | Rely on `native` |

**Refs:** `ref` is a string URI, e.g. `file:///…/tool-results/x.txt` or `aa-blob:{chunk_id}:{range}`. Clients may inline on read.

**Coverage intent:** almost all user-visible chat/tools map into the table above. Topology (Claude trees), encrypted reasoning, approvals/MCP chrome, compaction stay honest as `meta`/`raw_only`/`thought` without fake text — full detail in `native`.

---

## 6. `hot.meta.json` (tip version record)

Written/updated by ingest and prune. Example:

```json
{
  "format": "aa.stream",
  "v": 1,
  "v_min_present": 1,
  "v_max_present": 1,
  "agent": "Trip-G",
  "created_at": "2026-09-11T21:50:00Z",
  "updated_at": "2026-09-11T21:50:00Z",
  "hot_path": "hot.jsonl",
  "policy": {
    "max_bytes": 314572800,
    "keep_bytes": 104857600
  },
  "stream_seq_next": 1043,
  "backends": {
    "grok": {
      "session_id": "019fcd9d-…",
      "source_path": "/home/eric/.grok/sessions/…/updates.jsonl",
      "byte_offset": 87234912,
      "last_ts": 1789162000.0
    }
  },
  "notes": "SA-first; AA-TUI not cut over"
}
```

**This file is the hot tip’s version record:** what format family/version the tip claims, what versions appear in the current hot window, prune policy, and tailer high-water marks.

---

## 7. Manifest chunk row (archive version record)

Extend L1 manifest lines (additive) with:

```json
{
  "chunk_id": "…",
  "path": "chunks/….jsonl.zst",
  "format": "aa.stream",
  "v": 1,
  "v_min": 1,
  "v_max": 1,
  "from_ts": …,
  "until_ts": …,
  "plain_bytes": …,
  "zstd_bytes": …,
  "line_count": …,
  "sha256_plain": "…",
  "agent": "Trip-G",
  "source": "prune_hot",
  "sealed_at": "…"
}
```

Readers: list manifest → filter by ts → decompress → parse lines by each line’s `v`.

---

## 8. Versioning discipline (summary)

| What changes | Bump |
|--------------|------|
| New `backend` value, new `body.kind`, new optional field | no `v` bump; maybe `body_map_v` |
| Mapper quality only | `body_map_v` only |
| Required field removed/renamed; `ts` unit change | **`v` → 2** |
| New dialect only | new `native.schema` string |

**Document channel:** `docs/specs/aa_stream/HOT_FORMAT_v1.md` in agent-abide. When `v` bumps, add `HOT_FORMAT_v2.md` beside it and leave v1 immutable.

**In-band:** every line has `v`+`format`; `hot.meta.json` + manifest repeat the record for empty tips and sealed chunks.

---

## 9. Implementation order (unchanged product intent)

1. Emit `aa.stream` v1 into `hot.jsonl` (grok tailer first)
2. Prune/seal **only** AA hot (not backend files)
3. SA reads hot + chunks (L3)
4. Codex/claude tailers + richer mappers
5. AA-TUI / L4 still later

---

## 10. Worked mapping cheatsheet (non-normative)

| Native | class | phase | body.kind |
|--------|-------|-------|-----------|
| grok `user_message_chunk` | message | delta | text_delta |
| grok `agent_message_chunk` | message | delta | text_delta |
| grok `agent_thought_chunk` | thought | delta | thinking_delta |
| grok `tool_call` / update | tool | start/delta/end | tool_call / tool_result |
| grok `turn_completed` | meta | end | meta |
| codex `response_item/message` | message | full | text |
| codex `response_item/reasoning` (enc) | thought | full | raw_only or empty thinking |
| codex `custom_tool_call` | tool | full | tool_call |
| codex `token_usage_record` | usage | full | usage |
| claude `user` / `assistant` | message | full | text (+ parts if needed) |
| claude `system` | meta | full | meta |
| anything new | unknown | none | raw_only |

