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

## Cross-product law (Eric, 2026-09-22)

This **file → fold → paint unit → glass** model is not aa-TUI-only.

It is the target display architecture for:

| Product | Glass today | Same middle layer |
|---------|-------------|-------------------|
| **agent-abide TUI** | Textual | `paint_fold` / ChatState (this tree) |
| **Socratic Arena (SA)** | React conversation pane | same event→unit→policy ideas over aa.stream / hot |
| **thiasai** | app shell UI | same: durable stream atoms, display units, full vs snippet |

Implications:

- aa.stream / hot.jsonl stay the **file** language across products where possible.
- **Fold + DisplayPolicy** (full speech/thinking, snippet tools, banner system) is shared product law — not three one-off UIs.
- Live and history use the **same** fold; only the cursor/batching differs.
- Grouping decisions later (e.g. background-tool collapse) land in the fold layer once, then each glass paints.

aa-dev holds the line while the model hardens; SA and thiasai adopt the same contracts rather than inventing parallel ones.

## Wiring status (2026-09-22)

| Path | Status |
|------|--------|
| `paint_fold` + policies + tests | done |
| Tip catch-up mounts from fold | done (`REPLAY_FOLD` / `TIP_MOUNT`) |
| PageUp folds events then prepends widgets | done |
| Live `fold_event` into ChatState | done (widgets still via `_dispatch_event`) |
| Single painter-only path (no dual mount) | polish — dual path OK for stability |

Restart aa-dev TUI to pick up tip/PageUp fold wiring.

## Launch aa-dev TUI (not prod)

```bash
# From agent-abide-dev:
./scripts/launch_tui_dev.sh -a Trip-G -a Astro -t50
# or:
python3 tui/asdaaas_tui.py -a Trip-G -t50
```

Prod path `~/projects/agent-abide/tui/...` does **not** include paint_fold tip toast.
After tip load you should see a notify like:
`Replay (tip): N full + M tools + … paint · earliest@…`
