#!/usr/bin/env python3
"""Verify -t N catch-up selects exactly N history lines (not turns)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import (  # noqa: E402
    line_to_tui_event,
    select_tip_paint_lines,
    is_dialogue_speech,
    _event_session_update,
)

HOMES = {
    "Trip-G": Path("/home/eric/agents/Trip-G"),
    "Squiggy": Path("/home/eric/agents/LeviSmith/Squiggy"),
}


def tip_for(home: Path, n: int):
    hot = home / "asdaaas" / "history" / "hot.jsonl"
    size = hot.stat().st_size
    events = []
    for mib in (0.25, 1, 2, 8, 32):
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
        tip = select_tip_paint_lines(batch, n)
        if len(tip) >= n or seek == 0:
            events = batch
            break
    return select_tip_paint_lines(events, n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", action="append")
    ap.add_argument("--n", type=int, default=50)
    args = ap.parse_args()
    agents = args.agent or list(HOMES)
    rc = 0
    for name in agents:
        home = HOMES.get(name) or Path(name)
        tip = tip_for(home if name in HOMES else home, args.n)
        d = sum(1 for e in tip if is_dialogue_speech(e))
        tools = sum(
            1
            for e in tip
            if _event_session_update(e) in ("tool_call", "tool_call_update")
        )
        last = _event_session_update(tip[-1]) if tip else None
        ok = len(tip) == args.n
        if not ok:
            rc = 1
        print(
            f"[{'OK' if ok else 'FAIL'}] {name}: lines={len(tip)}/{args.n} "
            f"(dialogue={d} tools={tools}) last={last}"
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
