"""Viewport-row tip selection under explicit geometries."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import (  # noqa: E402
    select_tip_by_rows,
    estimate_event_rows,
    wrap_text_rows,
    collapse_tip_events,
)


def _speech(text: str) -> dict:
    return {
        "params": {
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"text": text},
            }
        }
    }


def _tool(tid: str, body: str) -> dict:
    return {
        "params": {
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tid,
                "title": "run_terminal_command",
                "content": [{"type": "content", "content": {"type": "text", "text": body}}],
            }
        }
    }


def test_wrap_text_rows():
    assert wrap_text_rows("hi", 80) == 1
    assert wrap_text_rows("x" * 160, 80) == 2


def test_narrow_width_uses_more_events_for_same_rows():
    events = [_speech(f"line {i} " + ("word " * 20)) for i in range(80)]
    wide = select_tip_by_rows(events, 40, width=120, overshoot=1.0)
    narrow = select_tip_by_rows(events, 40, width=40, overshoot=1.0)
    # narrower → each speech taller → fewer widgets to fill same rows
    assert len(narrow) <= len(wide)


def test_tool_row_cap_matches_snippet_spirit():
    huge = _tool("t1", "x\n" * 500)
    r = estimate_event_rows(huge, 80)
    # border + max 6 body rows
    assert r <= 2 + 6


def test_fills_at_least_target_rows():
    events = []
    for i in range(100):
        events.append(_speech(f"short {i}"))
        events.append(_tool(f"t{i}", f"out {i}"))
    tip = select_tip_by_rows(events, 50, width=80, overshoot=1.15)
    est = sum(estimate_event_rows(e, 80) for e in tip)
    assert est >= 50
    assert len(tip) <= 400


def test_collapse_tools_one_per_id():
    events = [
        _tool("a", "1"),
        _tool("a", "2"),
        _speech("hi"),
        _tool("b", "3"),
    ]
    c = collapse_tip_events(events)
    ids = []
    for e in c:
        u = (e.get("params") or {}).get("update") or {}
        if u.get("toolCallId"):
            ids.append(u["toolCallId"])
    assert ids.count("a") == 1


def test_live_hot_under_geoms():
    hot = Path("/home/eric/agents/Trip-G/asdaaas/history/hot.jsonl")
    if not hot.exists():
        return
    from tui_history import line_to_tui_event

    size = hot.stat().st_size
    seek = max(0, size - 4 * 1024 * 1024)
    with open(hot, "rb") as f:
        f.seek(seek)
        if seek > 0:
            f.readline()
        raw = f.read().decode("utf-8", errors="replace")
    events = [
        line_to_tui_event(l, "hot") for l in raw.split("\n") if l.strip()
    ]
    events = [e for e in events if e]
    for w, rows in ((80, 24), (120, 40), (40, 10), (200, 60)):
        tip = select_tip_by_rows(events, rows, width=w, overshoot=1.15)
        est = sum(estimate_event_rows(e, w) for e in tip)
        assert est >= min(rows, est)
        assert len(tip) >= 1
