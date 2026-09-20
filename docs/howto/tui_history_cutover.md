# TUI cutover: grok updates.jsonl → asdaaas/history (hot.jsonl)

*Sketch 2026-09-19 · template = SA’s AA stream path*

## Goal
Textual TUI should render conversation the way **Socratic Arena** does: from **asdaaas dual-history**, primarily **`history/hot.jsonl`** (aa.stream v1), not by tailing grok **`updates.jsonl`** as the conversation SoR.

Input (send message) can stay adapter inbox/outbox. **Display history** moves to history/.

## Today

| Layer | Current TUI | SA (template) |
|-------|-------------|---------------|
| Live tail | `updates.jsonl` (grok backend file) | `history/hot.jsonl` via `aa_stream_parser` |
| Parse | grok-shaped events in tui_adapter | `aa_stream_parser` / `updates_parser` dual |
| Send | asdaaas inbox/outbox | SA API → agent |
| Speech lean | optional / unused for main paint | memory packs from speech; SA uses hot for density |

Code anchor: `adapters/tui_adapter.py` header still says “Tail updates.jsonl”.

## Target

```text
User types in TUI
  → asdaaas inbox (unchanged)
Agent / system speaks
  → asdaaas dual-write speech.jsonl + hot.jsonl (unchanged)
TUI paint
  → tail + parse history/hot.jsonl  (NEW SoR for display)
  → optional: speech.jsonl for lean “dialogue only” mode later
```

Grok `updates.jsonl` remains **backend flight recorder** for the binary; TUI stops depending on it for UX.

## Phases

### P0 — Shared parser (steal, don’t fork forever)
- [ ] Port or vendor SA’s `aa_stream_parser.py` into agent-abide (`core/` or `adapters/`)
- [ ] Map aa.stream events → existing TUI display model (user/assistant/system/control lines)
- [ ] Unit tests: fixture hot.jsonl → expected turns (use Trip-G or synthetic)

### P1 — Live tail hot.jsonl
- [ ] Resolve path: `~/agents/<Name>/asdaaas/history/hot.jsonl` (same home resolve as speech)
- [ ] Replace updates tail loop in `tui_adapter` with hot tail (+ catch-up from EOF or last N)
- [ ] Handle rotation/prune (hot seal → chunks): reopen or follow manifest if needed
- [ ] Keep inbox send path unchanged

### P2 — Feature parity with current TUI
- [ ] Streaming deltas / partial assistant paint (hot has phase delta)
- [ ] Control/delay lines (SA already derives delay from tool text — match or simplify)
- [ ] Scroll / follow-tail (already on main: 9abe3a0)
- [ ] Context / health badges (may still read health.json — OK)

### P3 — Fallback & migration
- [ ] If no `history/hot.jsonl` (legacy agent): fall back to updates.jsonl or conversation.jsonl with banner
- [ ] Document: agents need dual-history enabled (Trip-G already; Jr may still be conversation.jsonl-only)

### P4 — Optional lean mode
- [ ] Flag or keybind: paint from **speech.jsonl** only (dialogue) vs full hot (tools/thoughts)
- [ ] Aligns with memory’s S01 vs expand story

### P5 — Cleanup
- [ ] Remove grok-path assumptions from TUI docs
- [ ] thiasai Open TUI unchanged (still launch_tui.sh); benefits automatically
- [ ] Do not delete updates.jsonl ingestion from asdaaas — only TUI display SoR changes

## Non-goals
- Rewrite Textual UI chrome
- Make TUI call SA HTTP
- Replace grok binary logging

## Success
1. Open TUI on Trip-G with history/ present → messages match what SA history shows for same agent (same hot file).
2. Kill grok updates path visibility in TUI; agent still runs; new turns appear from hot.
3. Jr without history/: graceful fallback, not crash.

## Work location
**agent-abide-dev** branch off `main` (e.g. `feat/tui-hot-jsonl`), then promote to prod agent-abide when green.

## First commit suggestion
`core/aa_stream_display.py` (parser) + tests + tui_adapter flag `TUI_HISTORY_SOURCE=hot|updates` default hot when file exists.
