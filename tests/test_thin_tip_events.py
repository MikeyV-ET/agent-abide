"""-t N catch-up is dialogue only; tools do not end the tip."""
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


def test_catchup_tool_budget_is_zero():
    assert tip_max_tools(50) == 0
    assert tip_max_tools(1) == 0


def test_tip_is_dialogue_only_no_trailing_tools():
    events = []
    for i in range(30):
        events.append(_ev("user_message_chunk", f"user {i}"))
        events.append(_ev("agent_message_chunk", f"agent {i}"))
        events.append(_ev("tool_call", tool_id=f"t{i}"))
        events.append(_ev("tool_call_update", tool_id=f"t{i}"))
    events.append(_ev("user_message_chunk", "ERIC_FINAL"))
    events.append(_ev("agent_message_chunk", "AGENT_FINAL"))
    for j in range(10):
        events.append(_ev("tool_call_update", tool_id=f"trail{j}"))
    out = select_tip_paint_lines(events, 20)
    assert all(is_dialogue_speech(e) for e in out), [
        _event_session_update(e) for e in out[-5:]
    ]
    assert len(out) == 20
    texts = []
    for e in out:
        c = (e.get("params") or {}).get("update", {}).get("content") or {}
        if isinstance(c, dict):
            texts.append(c.get("text", ""))
    assert "ERIC_FINAL" in texts
    assert "AGENT_FINAL" in texts
    assert out[-1] and "AGENT_FINAL" in str(
        ((out[-1].get("params") or {}).get("update") or {}).get("content")
    )


def test_live_agents_tip_ends_on_dialogue():
    for label, path in [
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
        tools = [
            e
            for e in out
            if _event_session_update(e) in ("tool_call", "tool_call_update")
        ]
        assert not tools, f"{label} still has {len(tools)} tools on tip"
        assert is_dialogue_speech(out[-1]), f"{label} tip does not end on dialogue"
        assert sum(1 for e in out if is_dialogue_speech(e)) == 50
