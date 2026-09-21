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
| `core/stream_adapters/claude.py` | map/wrap/tail_claude_once |
| `core/stream_adapters/__init__.py` | `tail_once_for_backend` |
| `aa_stream` shims | `tail_grok_once` still importable |
| `full_stream_hook` docstring | orchestration role |
| Tests | 9 passed stream + parser tui |
| TUI live tail prefers hot.jsonl | **P1 done (auto)** |

## TODO after compaction (implement)

### 1. Wire hook to agent backend
- [x] `full_stream_hook._do_tail` use `tail_once_for_backend(backend, …)`  
- [x] Resolve `backend` from agents.json / asdaaas config (not hardcode grok)

### 2. Claude adapter (Astro)
- [x] `find_live_session` (dash-cwd + agents.json / health; locator optional)  
- [x] `map_claude_event` from Claude session jsonl shapes  
- [x] `tail_claude_once` → same checkpoint + `append_hot_events`  
- [x] `tail_stream` / `tail_backend` / legacy `tail_grok` all enable

### 3. TUI P1 — live paint from hot
- [x] `tui/asdaaas_tui.py`: tail `history/hot.jsonl` when present  
- [x] `TUI_HISTORY_SOURCE=auto|hot|updates`  
- [x] Catch-up: replay hot lines via `aa_event_to_tui_update` → dispatch  
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


## Implemented post-compaction (2026-09-19 ~22:25 PDT)

- `full_stream_hook`: `resolve_agent_backend`, `_stream_tail_enabled`, `_do_tail` → `tail_once_for_backend`
- `stream_adapters.claude`: real adapter; smoke on Astro session path
- TUI P1: `_resolve_display_history` + aa.stream→grok update bridge; Claude waits for hot
- Tests: 22 passed (stream + parser + claude + tui bridge + hook)


## Architecture correction (Eric 2026-09-19 ~23:20)

**Intent (locked):** acquisition + normalization live **inside** AA backends
(`GrokBackend` / `ClaudeBackend`), each emitting aa.stream into hot.

What we built (`stream_adapters.*` + hook dispatch) is the right *logic* but the
wrong *owner*. Hook/adapters stay as helpers; backends must own the call.

Claude path must **not** go through a grok-like intermediate. Both backends → aa.stream.


## Backend ownership landed (2026-09-19 ~23:30)

- `GrokBackend.configure_aa_history` + `sync_hot_stream` → `stream_adapters.grok.tail_grok_once`
- Called from `_process_update_frames`, `refresh_tokens`, and `TurnEngine` post-deliver
- `history/config.json` `owner=backend` (default); `owner=hook` keeps sidecar
- Trip-G config set to `owner=backend`
- ClaudeBackend still needs the same wire-up (adapter exists; configure not yet)

## Viewport-row tip (`-t`) — 2026-09-21

`-t N` means **about N terminal rows** of history at the chat width, not N
turns or N widgets. Catch-up walks `hot.jsonl` backward, estimates rows per
event (`estimate_event_rows`), stops when the budget is filled (small overshoot).

Geometry for tests without Eric's glass:

```bash
python3 scripts/run_with_geom.py 120 40 -- python3 scripts/verify_tui_tip.py --width 120 --rows 40
```

Isolated tmux also works: `tmux -S /tmp/t new -d -x 120 -y 40 …`
