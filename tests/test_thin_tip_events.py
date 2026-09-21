"""-t N = N TUI paint lines (not N dialogue turns)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import (  # noqa: E402
    select_tip_paint_lines,
    thin_tip_events,
    is_dialogue_speech,
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


def test_t50_means_fifty_paint_lines():
    events = []
    for i in range(40):
        events.append(_ev("user_message_chunk", f"u{i}"))
        events.append(_ev("agent_message_chunk", f"a{i}"))
        events.append(_ev("tool_call", tool_id=f"t{i}"))
        events.append(_ev("tool_call_update", tool_id=f"t{i}"))
        events.append(_ev("tool_call_update", tool_id=f"t{i}"))
        events.append(_ev("task_completed"))
        events.append(_ev("turn_completed"))
        events.append(_ev("user_message_chunk", "[continue (id=x)] Your turn ended. stand by."))
    out = select_tip_paint_lines(events, 50)
    assert len(out) == 50, len(out)
    types = [
        (e.get("params") or {}).get("update", {}).get("sessionUpdate") for e in out
    ]
    assert "task_completed" not in types
    assert "turn_completed" not in types
    # tools collapsed to 1 per id
    assert types.count("tool_call") + types.count("tool_call_update") <= 50


def test_thin_tip_alias_is_paint_lines():
    events = [_ev("agent_message_chunk", f"x{i}") for i in range(100)]
    assert len(thin_tip_events(events, 50)) == 50


def test_squiggy_hot_t50_at_most_50_paint():
    hot = Path("/home/eric/agents/LeviSmith/Squiggy/asdaaas/history/hot.jsonl")
    if not hot.exists():
        return
    from tui_history import line_to_tui_event

    # 1 MiB tip window is enough density for 50 paint lines
    size = hot.stat().st_size
    read_size = min(size, 1 * 1024 * 1024)
    seek = max(0, size - read_size)
    with open(hot, "rb") as f:
        f.seek(seek)
        if seek > 0:
            f.readline()
        raw = f.read().decode("utf-8", errors="replace")
    events = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        ev = line_to_tui_event(line, "hot")
        if ev:
            events.append(ev)
    out = select_tip_paint_lines(events, 50)
    assert len(out) == 50, len(out)
    assert sum(1 for e in out if is_tip_paint_event(e)) == 50
