#!/usr/bin/env python3
"""Verify -t catch-up tip ends on dialogue (no tool stack at bottom).

Usage:
  python3 scripts/verify_tui_tip.py
  python3 scripts/verify_tui_tip.py --agent Squiggy --n 50

This is the offline half of TUI verification. Live TUI still needs a human
or a future headless Textual driver; this locks the *selection* contract so
we stop shipping "8 tools at the end" by accident.
"""
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
        d = sum(1 for e in tip if is_dialogue_speech(e))
        if d >= n or seek == 0:
            events = batch
            break
    return select_tip_paint_lines(events, n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", action="append", default=None)
    ap.add_argument("--n", type=int, default=50)
    args = ap.parse_args()
    agents = args.agent or list(HOMES)
    rc = 0
    for name in agents:
        home = HOMES.get(name) or Path(name)
        tip = tip_for(home if name in HOMES else home, args.n)
        tools = [
            e
            for e in tip
            if _event_session_update(e) in ("tool_call", "tool_call_update")
        ]
        d = sum(1 for e in tip if is_dialogue_speech(e))
        last = _event_session_update(tip[-1]) if tip else None
        last_txt = ""
        if tip:
            c = ((tip[-1].get("params") or {}).get("update") or {}).get("content")
            if isinstance(c, dict):
                last_txt = str(c.get("text") or "")[:80].replace("\n", " ")
        ok = d == args.n and not tools and last in (
            "user_message_chunk",
            "agent_message_chunk",
            "agent_thought_chunk",
        )
        status = "OK" if ok else "FAIL"
        if not ok:
            rc = 1
        print(f"[{status}] {name}: dialogue={d}/{args.n} tools={len(tools)} last={last}")
        print(f"         last_text={last_txt!r}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
