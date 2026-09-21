"""-t N = N dialogue; tools are a small independent garnish, not 1:1."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import (  # noqa: E402
    select_tip_paint_lines,
    tip_max_tools,
    is_dialogue_speech,
    _event_session_update,
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


def test_tool_cap_not_one_to_one_with_dialogue():
    assert tip_max_tools(50) == 8
    assert tip_max_tools(50) < 50
    assert tip_max_tools(12) <= 8


def test_t50_not_fifty_plus_fifty():
    events = []
    for i in range(80):
        events.append(_ev("user_message_chunk", f"user {i}"))
        events.append(_ev("agent_message_chunk", f"agent {i}"))
        for j in range(5):
            events.append(_ev("tool_call", tool_id=f"t{i}_{j}"))
            events.append(_ev("tool_call_update", tool_id=f"t{i}_{j}"))
    out = select_tip_paint_lines(events, 50)
    d = sum(1 for e in out if is_dialogue_speech(e))
    tools = sum(
        1
        for e in out
        if _event_session_update(e) in ("tool_call", "tool_call_update")
    )
    assert d == 50, d
    assert tools <= tip_max_tools(50), tools
    assert tools < 50, "must not imply 1:1 dialogue/tool parity"
    assert len(out) < 70, len(out)


def test_tip_does_not_end_on_tool_pile():
    events = []
    for i in range(20):
        events.append(_ev("agent_message_chunk", f"agent {i}"))
    events.append(_ev("user_message_chunk", "ERIC_FINAL question"))
    events.append(_ev("agent_message_chunk", "AGENT_FINAL answer"))
    for j in range(15):
        events.append(_ev("tool_call", tool_id=f"trail{j}"))
        events.append(_ev("tool_call_update", tool_id=f"trail{j}"))
    out = select_tip_paint_lines(events, 10)
    # last dialogue should appear; trailing tools at most 2
    texts = []
    for e in out:
        c = (e.get("params") or {}).get("update", {}).get("content") or {}
        if isinstance(c, dict) and c.get("text"):
            texts.append(c["text"])
    assert "ERIC_FINAL" in " ".join(texts)
    assert "AGENT_FINAL" in " ".join(texts)
    # find last dialogue index
    last_d = -1
    for i, e in enumerate(out):
        if is_dialogue_speech(e):
            last_d = i
    trailing = out[last_d + 1 :]
    assert len(trailing) <= 2, trailing


def test_tripg_hot_not_fifty_fifty():
    hot = Path("/home/eric/agents/Trip-G/asdaaas/history/hot.jsonl")
    if not hot.exists():
        return
    from tui_history import line_to_tui_event

    size = hot.stat().st_size
    seek = max(0, size - 8 * 1024 * 1024)
    with open(hot, "rb") as f:
        f.seek(seek)
        if seek > 0:
            f.readline()
        raw = f.read().decode("utf-8", errors="replace")
    events = [
        line_to_tui_event(l, "hot") for l in raw.split("\n") if l.strip()
    ]
    events = [e for e in events if e]
    out = select_tip_paint_lines(events, 50)
    d = sum(1 for e in out if is_dialogue_speech(e))
    tools = sum(
        1
        for e in out
        if _event_session_update(e) in ("tool_call", "tool_call_update")
    )
    assert d == 50, d
    assert tools <= 8, tools
