# Resume after compaction — stream adapters + TUI hot cutover

*Eric 2026-09-19: document now; finish implement after compaction.*

## Branch
`agent-abide-dev` **`feat/tui-hot-jsonl`**

Commits already on branch:
- `3d48f49` — TUI P0: aa_stream_parser + tui_history resolve (prefer hot.jsonl)
- `e1ec2a9` — partition grok normalize → `stream_adapters.grok`
- `8a3a884` — FORMAT_V import fix

## Architecture (locked intent)
```text
Backend-specific adapter (grok | claude)
  tail native session file
  map/wrap → aa.stream events
       ↓
aa_stream.append_hot_events → history/hot.jsonl

Frontends (TUI, SA) READ hot (and speech) — do not write history.
```

## Done
| Item | Status |
|------|--------|
| `core/stream_adapters/grok.py` | map/wrap/tail_grok_once |
| `core/stream_adapters/claude.py` | **stub only** |
| `core/stream_adapters/__init__.py` | `tail_once_for_backend` |
| `aa_stream` shims | `tail_grok_once` still importable |
| `full_stream_hook` docstring | orchestration role |
| Tests | 9 passed stream + parser tui |
| TUI live tail still on updates.jsonl | **not switched yet** |

## TODO after compaction (implement)

### 1. Wire hook to agent backend
- [ ] `full_stream_hook._do_tail` use `tail_once_for_backend(backend, …)`  
- [ ] Resolve `backend` from agents.json / asdaaas config (not hardcode grok)

### 2. Claude adapter (Astro)
- [ ] `find_live_session` via `api/session_locator.py`  
- [ ] `map_claude_event` from Claude session jsonl shapes  
- [ ] `tail_claude_once` → same checkpoint + `append_hot_events`  
- [ ] Enable `tail_grok`-style config flag or generic `tail_backend` for Astro

### 3. TUI P1 — live paint from hot
- [ ] `tui_adapter` / `tui/asdaaas_tui.py`: tail `history/hot.jsonl` when present  
- [ ] `TUI_HISTORY_SOURCE=auto|hot|updates`  
- [ ] Catch-up on start via `entries_from_hot` + `entry_to_tui_lines`  
- [ ] Fallback if no history/ (Jr-style conversation.jsonl)

### 4. Cleanup
- [ ] Reduce shim surface once callers migrated  
- [ ] Push branch when remote available  
- [ ] Promote to prod agent-abide when green

## Do not
- Write history from TUI/SA  
- Make claude binary write hot.jsonl directly  

## Quick verify
```bash
cd ~/projects/agent-abide-dev
git checkout feat/tui-hot-jsonl
PYTHONPATH=core python3 -m pytest tests/test_aa_stream_hot.py tests/test_aa_stream_parser_tui.py -q
```
