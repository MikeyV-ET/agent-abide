#!/usr/bin/env python3
"""Verify viewport-row tip selection under explicit geometry.

Usage:
  python3 scripts/verify_tui_tip.py
  python3 scripts/verify_tui_tip.py --width 120 --rows 40
  python3 scripts/run_with_geom.py 200 60 -- python3 scripts/verify_tui_tip.py --rows 60 --width 200
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import (  # noqa: E402
    line_to_tui_event,
    select_tip_by_rows,
    estimate_event_rows,
    collapse_tip_events,
)

HOMES = {
    "Trip-G": Path("/home/eric/agents/Trip-G"),
    "Squiggy": Path("/home/eric/agents/LeviSmith/Squiggy"),
}


def load_events(hot: Path, max_mib: float = 8.0):
    size = hot.stat().st_size
    events = []
    for mib in (0.25, 1, 2, 8, 32):
        if mib > max_mib and events:
            break
        seek = max(0, size - int(mib * 1024 * 1024))
        with open(hot, "rb") as f:
            f.seek(seek)
            if seek > 0:
                f.readline()
            raw = f.read().decode("utf-8", errors="replace")
        batch = []
        for line in raw.split("\n"):
            if not line.strip():
                continue
            ev = line_to_tui_event(line, "hot")
            if ev:
                batch.append(ev)
        tip = select_tip_by_rows(batch, 1, width=80)  # force progress
        events = batch
        # enough collapsed material?
        if len(collapse_tip_events(batch)) > 30 or seek == 0:
            if mib >= 2:
                break
    return events


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", action="append")
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--rows", type=int, default=0)
    ap.add_argument("--overshoot", type=float, default=1.15)
    args = ap.parse_args()
    width = args.width or int(__import__("shutil").get_terminal_size((80, 24)).columns)
    rows = args.rows or int(__import__("shutil").get_terminal_size((80, 24)).lines)
    # chat fudge like TUI
    chat_w = max(40, width - 4)
    target = max(8, rows - 6) if not args.rows else max(4, args.rows)

    agents = args.agent or list(HOMES)
    rc = 0
    print(f"geometry: term={width}x{rows} chat_w={chat_w} target_rows={target}")
    for name in agents:
        home = HOMES.get(name) or Path(name)
        hot = (home if name in HOMES else home) / "asdaaas/history/hot.jsonl"
        if not hot.exists():
            print(f"[SKIP] {name}: no hot")
            continue
        events = load_events(hot)
        tip = select_tip_by_rows(
            events, target, width=chat_w, overshoot=args.overshoot
        )
        est = sum(estimate_event_rows(e, chat_w) for e in tip)
        # must reach ~target (allow under if file short)
        ok = est >= min(target, est) and (
            est >= target * 0.85 or len(collapse_tip_events(events)) < 5
        )
        # overshoot bound
        if est > target * args.overshoot * 2 + 20:
            ok = False
        if not ok:
            rc = 1
        print(
            f"[{'OK' if ok else 'FAIL'}] {name}: widgets={len(tip)} "
            f"est_rows={est} target={target} width={chat_w}"
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
