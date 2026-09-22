# Paint fold (hot → display model)

**aa-dev only.** Shared translator for live tail and history.

## Layers

| Layer | Atom | Module |
|-------|------|--------|
| File | jsonl / aa.stream event | `hot.jsonl` |
| Fold | Paint unit (ChatState item) | `tui/paint_fold.py` + `tui/chat_model.py` |
| Glass | Textual widget | `tui/chat_widgets.py` |

## Display policy (Eric)

| Kind | Policy |
|------|--------|
| User / agent speech, thinking | **full** |
| Tools | **snippet** (truncated; expand in UI) |
| Retry / system / aa.control | **banner** |
| turn_completed, background_tasks list, … | **drop** |

## API

```python
from paint_fold import ChatState, fold_hot_lines, enough_for_tip, paint_units

state = ChatState()
fold_hot_lines(state, lines_from_file)
if enough_for_tip(state):
    for item, policy in paint_units(state):
        ...
```

History should stop on **paint counts** (`enough_for_tip`), not raw byte quotas alone.
Live path should call the same `fold_event` / `apply_event` as history.
