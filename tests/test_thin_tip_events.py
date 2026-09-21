"""-t N = N history lines (not turns, not a tool quota)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import (  # noqa: E402
    select_tip_paint_lines,
    is_dialogue_speech,
    is_tip_paint_event,
    _event_session_update,
    line_to_tui_event,
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


def test_t50_is_fifty_lines_mixed():
    events = []
    for i in range(40):
        events.append(_ev("user_message_chunk", f"u{i}"))
        events.append(_ev("agent_message_chunk", f"a{i}"))
        events.append(_ev("tool_call", tool_id=f"t{i}"))
        events.append(_ev("tool_call_update", tool_id=f"t{i}"))
        events.append(_ev("tool_call_update", tool_id=f"t{i}"))
        events.append(_ev("task_completed"))
    out = select_tip_paint_lines(events, 50)
    assert len(out) == 50
    assert all(is_tip_paint_event(e) for e in out)
    types = [_event_session_update(e) for e in out]
    assert "task_completed" not in types
    # tools collapsed: at most one entry per id in the window
    tool_ids = []
    for e in out:
        if _event_session_update(e) in ("tool_call", "tool_call_update"):
            u = (e.get("params") or {}).get("update") or {}
            tool_ids.append(u.get("toolCallId"))
    assert len(tool_ids) == len(set(tool_ids))


def test_lines_not_turns():
    """Dense tools: 50 lines can be fewer than 50 dialogue turns."""
    events = []
    for i in range(10):
        events.append(_ev("agent_message_chunk", f"speech {i}"))
        for j in range(10):
            events.append(_ev("tool_call_update", tool_id=f"t{i}_{j}"))
    out = select_tip_paint_lines(events, 50)
    assert len(out) == 50
    d = sum(1 for e in out if is_dialogue_speech(e))
    assert d < 50  # tools count as lines too


def test_chronological_and_exact_n():
    events = [_ev("agent_message_chunk", f"m{i}") for i in range(100)]
    out = select_tip_paint_lines(events, 50)
    assert len(out) == 50
    texts = [
        ((e.get("params") or {}).get("update") or {}).get("content", {}).get("text")
        for e in out
    ]
    assert texts[0] == "m50"
    assert texts[-1] == "m99"


def test_live_hot_exactly_n_lines():
    for name, path in [
        ("Trip-G", Path("/home/eric/agents/Trip-G/asdaaas/history/hot.jsonl")),
        ("Squiggy", Path("/home/eric/agents/LeviSmith/Squiggy/asdaaas/history/hot.jsonl")),
    ]:
        if not path.exists():
            continue
        size = path.stat().st_size
        seek = max(0, size - 8 * 1024 * 1024)
        with open(path, "rb") as f:
            f.seek(seek)
            if seek > 0:
                f.readline()
            raw = f.read().decode("utf-8", errors="replace")
        events = [
            line_to_tui_event(l, "hot") for l in raw.split("\n") if l.strip()
        ]
        events = [e for e in events if e]
        out = select_tip_paint_lines(events, 50)
        assert len(out) == 50, f"{name} got {len(out)}"
