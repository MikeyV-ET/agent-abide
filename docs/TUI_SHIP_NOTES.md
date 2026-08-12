# TUI perf + modularization — ship notes

**Branch:** `tui/perf-and-modularize`  
**Base:** `main` @ be801f6  
**Owner:** Trip-G / Eric dogfood  

## What shipped on this branch (intent)

### Performance (Phase A)
- Tool panels default to **4-line snippet**, expand on click (Eric: thinking > tools)
- Cap stored tool output (64KB)
- Coalesce `updates.jsonl` batches (`event_coalesce.py`): merge stream chunks, latest-wins tool updates
- Streaming `refresh()` without full layout thrash; respect scroll-up (`_following_tail`)
- Header telemetry **immediate** on agent tab switch (`status_read.py`)
- Spinner 4 Hz; profile turn count cached

### Modularization (Phase B)
| Module | Responsibility |
|--------|----------------|
| `theme.py` | Palettes, Theme proxy, `set_theme` |
| `chat_widgets.py` | Tool/message/thinking panels |
| `message_input.py` | Input bar (paste tests import via asdaaas_tui re-export) |
| `chrome_widgets.py` | ContentScroll, alerts, turn separators |
| `nav_widgets.py` | Tabs, room msgs, footer, theme selector |
| `chat_model.py` | Pure ChatState + `apply_event` |
| `chat_events.py` | Event field helpers |
| `event_coalesce.py` | Batch coalesce |
| `status_read.py` | health/gaze → telemetry |
| `tui_env.py` | Injectable paths |
| `asdaaas_tui.py` | App composition (~3.5k, down from ~4.3k) |

Dual-path: every dispatch updates pure `ChatState` then widgets.

## How to dogfood
```bash
# stop current TUI if needed, then:
python3 ~/projects/agent-abide-dev/tui/asdaaas_tui.py -a Squiggy -o eric
# multi-agent example if supported by your launch habit:
# python3 .../asdaaas_tui.py -a Squiggy -a Sr -o eric
```
Agents keep running prod `core/`; only TUI binary path changes.

## Tests
```bash
cd ~/projects/agent-abide-dev
python3 -m pytest tests/test_event_coalesce.py tests/test_chat_model.py \
  tests/test_chat_shadow.py tests/test_chat_events.py tests/test_status_read.py \
  tests/test_tui_env.py tests/test_ephact.py tests/test_tui_paste.py \
  tests/test_tui_gaze.py -q
```
Expect **62 passed**.

## Promote to prod
1. PR or merge `tui/perf-and-modularize` → `main`
2. FF `~/projects/agent-abide` main
3. Relaunch TUI only (no agent restart required)

## Open / not done
- Eric dogfood confirmation (A8/B6)
- Widgets not yet *driven by* ChatState (shadow only)
- Phase C windowed DOM only if lag remains after dogfood
- `test_observer_stdout.py` still only on old phase1 branch (unrelated)

## Display policy (do not regress)
- Thinking almost always fully visible
- Tools snippet+expand by default
- Smooth UI **and** usable history both required
- Paste + ephact keep/improve
