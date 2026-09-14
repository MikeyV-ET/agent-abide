# AUDIT: Backend session schemas → V2 hot/archive design

**When:** 2026-09-11 ~14:25 PDT  
**Sources (local, primary):**
- Codex rollout: `~/.codex/sessions/2026/09/11/rollout-2026-09-11T14-10-38-01a0924e-….jsonl` (~2.3 MB, Eric’s chat)
- Codex lean: `~/.codex/history.jsonl`, `session_index.jsonl`
- Codex app-server schema dump: `/tmp/codex-app-server-schema.Skm9jL/` (**treat as generated export**, not sacred docs — pair with published)
- Claude: `~/.claude/projects/-home-eric-agents-testagent/<uuid>.jsonl` + `subagents/`, `tool-results/`
- Grok: `~/.grok/sessions/<urlencoded-cwd>/<uuid>/updates.jsonl`

**Published (secondary, pair with local):**
- Claude Code directory / transcripts: https://code.claude.com/docs/en/claude-directory (+ community JSONL field refs)
- Codex app-server: https://developers.openai.com/codex/app-server (JSON-RPC threads; rollout on disk)
- Codex threads API notes: mintlify openai/codex threads (rollout path field)
- Schema origin: `openai/codex` app-server-protocol (v1/v2); dump matches JSON-Schema draft-07 exports

---

## 1. On-disk layouts (three backends)

### Grok (current AA default)
```
~/.grok/sessions/<URL-encoded-cwd>/<session-uuid>/
  updates.jsonl          # full stream (hot today)
  conversation.jsonl?    # sometimes; AA uses agent-home V1 instead
  stdout_log.jsonl       # huge; not speech
  events.jsonl
  compaction/ …
```
- **Envelope:** `{timestamp, method:"session/update", params:{update:{sessionUpdate, …}}}`
- **Kinds:** tool_call*, agent_message_chunk, agent_thought_chunk, user_message_chunk, turn_completed, compaction_*, subagent_*, …

### Codex CLI
```
~/.codex/
  history.jsonl                    # LEAN user prompts only {session_id, ts, text}
  session_index.jsonl              # {id, thread_name, updated_at}
  sessions/YYYY/MM/DD/
    rollout-<ts>-<uuid>.jsonl      # FULL stream (append-only)
  *.sqlite                         # logs, state, goals, queue (ops — not our V2 body)
```
- **Rollout envelope:** `{timestamp, ordinal, type, payload}`
- **Outer types seen:** `session_meta`, `event_msg`, `response_item`, `token_usage_record`, `turn_context`, `world_state`
- **payload.type examples:** message, reasoning, custom_tool_call(+output), task_started/complete, item_completed, token_count, …
- **App-server wire (schema dump):** JSON-RPC methods `thread/*`, notifications `Thread/*`, `ItemStarted/Completed`, `AgentMessageDelta`, …  
  On-disk rollout is the **persistence** of that thread; app-server is the **live API** over it. v1 vs v2 schema files both present in dump — prefer v2 + live rollout samples when they disagree.

### Claude Code
```
~/.claude/
  history.jsonl                    # global typed-prompt history (lean-ish)
  projects/<cwd-with-/-to-- >/
    <session-uuid>.jsonl           # FULL transcript (append-only)
    <session-uuid>/
      subagents/agent-*.jsonl
      tool-results/*.txt           # spilled large tool output
  sessions/                        # small pid/key side files, not the transcript
```
- **Envelope:** flat event with `type` discriminator + `sessionId` + often `uuid`/`parentUuid`/`timestamp`
- **Types seen:** user, assistant, system, attachment, file-history-snapshot, mode, permission-mode, ai-title, …
- **Tree:** parentUuid links; subagents are **sidecar files**, not only inline
- **Published:** matches local layout; cleanupPeriodDays can delete old transcripts (retention risk for archive!)

---

## 2. What rhymes (design invariants)

| Pattern | Grok | Codex | Claude |
|---------|------|-------|--------|
| Append-only JSONL full stream | updates.jsonl | rollout-*.jsonl | `<sid>.jsonl` |
| Separate lean user text | AA conversation.jsonl (V1) | history.jsonl | history.jsonl (global) |
| Session/thread id | uuid dir name | session_id in meta + filename | sessionId field + filename |
| cwd binding | encoded in parent path | in session_meta.payload.cwd | cwd field + project folder |
| Tool I/O can be huge | inline (and stdout_log) | inline in rollout | often **spilled** to tool-results/ |
| Live protocol ≠ file | JSON-RPC session/update | app-server thread/* | stream-json stdio |
| Compaction/rollback | compaction checkpoints | thread/rollback as **append marker** (not rewrite) | own context mgmt |

**Takeaway:** Everyone already has “hot JSONL + optional lean index.” Nobody’s file is a good **shared** hot tip. Our V2 should **own** the tip and treat these as **sources**.

---

## 3. Suspicion notes on `/tmp/codex-app-server-schema.Skm9jL/`

| Check | Result |
|-------|--------|
| Looks like | JSON-Schema export of app-server protocol (ClientRequest, ServerNotification, v1/, v2/, bundled .schemas.json) |
| Matches live? | Concepts match rollout (thread/turn/item, message deltas, exec approvals). Rollout **on-disk** types are a **superset/sibling** (`session_meta`, `ordinal`, …) not identical to every notification name |
| Docs pointer? | Almost no URLs inside dump (json-schema.org + one reasoning doc). **Not** a doc index — pair with https://developers.openai.com/codex/app-server and openai/codex source |
| Trust | Good for **method/notification names and shapes** when wiring app-server. Prefer **rollout samples** for archive/hot ingest from CLI files |

---

## 4. Implications for **our** hot file (backend-agnostic)

### Recommended ownership (locks option C→B from earlier chat)

```
{agent_home}/asdaaas/history/
  hot.jsonl                 # OUR tip (300→100 policy applies HERE only)
  manifest.jsonl
  chunks/*.jsonl.zst
  sources/                  # optional bookmarks: last offset per backend path
```

**Do not** prune/rewrite grok/codex/claude native files.

### Envelope (L2 sketch — backend-agnostic)

```json
{
  "ts": 1789161040.409,          // unix seconds (float ok)
  "ts_iso": "2026-09-11T21:10:42.409Z",
  "agent": "Trip-G",
  "session_id": "…",             // backend session/thread id
  "backend": "grok|codex|claude|asdaaas",
  "kind": "normalized-or-passthrough",  // see below
  "source": {
    "path": "/abs/…/rollout-….jsonl",
    "offset": 123456,
    "ordinal": 9                 // if present
  },
  "payload": { /* backend-native event OR normalized */ }
}
```

**Phase 1 (L1.5 / L3 SA):** `payload` = **passthrough** native line object (or whole grok update). SA learns to render per `backend`.  
**Phase 2:** optional `kind` normalization (`user_text`, `agent_text`, `tool_call`, `tool_result`, `thought`, `meta`) for shared UI — never delete native payload.

### Ingest adapters (one tailer each)

| Backend | Watch | Map |
|---------|-------|-----|
| grok | `updates.jsonl` | ts ← timestamp; session ← dir; payload ← line |
| codex | `sessions/**/rollout-*.jsonl` + session_index | ts ← timestamp; session ← meta/id; payload ← line; ordinal |
| claude | `projects/**/<uuid>.jsonl` | ts ← timestamp; session ← sessionId; follow tool-results by ref if needed |

asdaaas already has `ClaudeBackend` (stdio stream-json) and `GrokBackend`; Codex would be a third **runtime** backend later — **file ingest does not require** full runtime integration first.

### Lean vs full (aligns V1/V2)

| Layer | Ours | Codex analog | Claude analog |
|-------|------|--------------|---------------|
| V1 speech | `asdaaas/conversation.jsonl` | `~/.codex/history.jsonl` (user-only, thinner) | prompts in history.jsonl |
| V2 full | `history/hot+chunks` | rollout-*.jsonl | projects/…/*.jsonl |

Codex/Claude history.jsonl are **not** sufficient as V1 (missing agent speech). Keep our V1 writer.

### Subagents / spills

- Claude: must index `subagents/*.jsonl` and optionally `tool-results/*` or V2 loses tool bodies  
- Grok: subagent events often inline; stdout_log separate landmine  
- Codex: tool payloads inline in rollout in sample; watch for future spill

### Retention landmine

Claude **deletes** old project transcripts by default (cleanupPeriodDays≈30). **Ingest to our archive promptly** or history vanishes. Codex/Grok less aggressive in samples.

---

## 5. What this means for the open hot-file decision

| Question | Answer from this audit |
|----------|------------------------|
| Where is the 300 MB file? | **Should be ours:** `history/hot.jsonl` |
| Always backend native? | **No** — native files are sources; three incompatible layouts |
| Backend-agnostic hot? | **Yes** — shared envelope + passthrough payload; prune only our hot |
| Schema dump role | Wire-protocol reference for Codex app-server; not the on-disk rollout schema alone |
| SA L3 | Read our hot+chunks; switch on `backend` for render; codeblocks stay SA strength |

---

## 6. Suggested next engineering steps

1. Lock hot path = `history/hot.jsonl`; disable prune of backend paths in CLI (guard).  
2. Implement `tail_backend → append hot` for grok first (existing live), then codex rollout, then claude project jsonl.  
3. L2 METHOD subsection: envelope above.  
4. SA L3: history API sources from full_stream; renderer registry by backend.  
5. Optional: copy schema dump into `agent-abide/docs/third_party/codex-app-server-schema/` with README pointing at OpenAI docs (don’t trust /tmp alone).

