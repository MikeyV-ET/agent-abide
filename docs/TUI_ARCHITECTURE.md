# asdaaas TUI architecture (in progress)

Branch work: `tui/perf-and-modularize`. Display policy: see Trip-G METHOD.

## Target shape
```
EventSource (file tail / API)
  → coalesce_events (event_coalesce.py)
  → ChatState / apply_event (chat_model.py)   # pure
  → AsdaaasTUI widgets (asdaaas_tui.py)       # Textual shell
TuiEnv (tui_env.py)                           # injectable paths
```

## Modules today
| File | Role |
|------|------|
| `tui/asdaaas_tui.py` | Textual App, widgets, workers, still owns live mount path |
| `tui/event_coalesce.py` | Batch merge before main-thread apply |
| `tui/chat_events.py` | Pure event field accessors |
| `tui/chat_model.py` | Pure ChatState + apply_event reducer |
| `tui/tui_env.py` | Injectable agents_home / asdaaas paths |
| `tui/ephact_*.py` | Ephact parser/viewer |

## Display policy (summary)
- Thinking: almost always fully visible
- Tools: snippet default, expand on click
- Smooth UI and usable history both required

## Dual-path dispatch (2026-08-04)
`AsdaaasTUI._dispatch_event` now:
1. `apply_event(chat_state, event)` on per-agent pure `ChatState`
2. Existing widget mount/update path (unchanged UX)

`Config.get_env()` / `set_env(TuiEnv)` — CLI sets env from `--agents-home`.
`agent_home` falls back through `TuiEnv` when agents.json has no `home`.
