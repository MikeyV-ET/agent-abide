"""Legacy name: tip selection is viewport-row based."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import (  # noqa: E402
    select_tip_by_rows,
    select_tip_paint_lines,
    estimate_event_rows,
    is_tip_paint_event,
)


def _ev(su: str, text: str = "", tool_id: str = "") -> dict:
    u = {"sessionUpdate": su}
    if text:
        u["content"] = {"text": text}
    if tool_id:
        u["toolCallId"] = tool_id
        u["status"] = "completed" if su == "tool_call_update" else "running"
        u["title"] = "run"
    return {"params": {"update": u}}


def test_paint_lines_row_mode_fills_rows_not_widget_count():
    events = []
    for i in range(40):
        events.append(_ev("user_message_chunk", f"u{i}"))
        events.append(_ev("agent_message_chunk", f"a{i}"))
        events.append(_ev("tool_call_update", tool_id=f"t{i}"))
    out = select_tip_paint_lines(events, 50, width=80)  # 50 rows
    est = sum(estimate_event_rows(e, 80) for e in out)
    assert est >= 50
    assert all(is_tip_paint_event(e) for e in out)


def test_widget_mode_exact_n():
    events = [_ev("agent_message_chunk", f"m{i}") for i in range(100)]
    out = select_tip_paint_lines(events, 50, row_mode=False)
    assert len(out) == 50


def test_by_rows_chronological():
    events = [_ev("agent_message_chunk", f"m{i}") for i in range(30)]
    out = select_tip_by_rows(events, 10, width=80, overshoot=1.0)
    texts = [
        ((e.get("params") or {}).get("update") or {}).get("content", {}).get("text")
        for e in out
    ]
    assert texts == sorted(texts, key=lambda t: int(t[1:]))
