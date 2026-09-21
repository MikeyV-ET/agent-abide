"""-t N = N dialogue on the tip; tools collapsed and capped."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import (  # noqa: E402
    select_tip_paint_lines,
    is_dialogue_speech,
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


def test_t50_keeps_fifty_dialogue_not_just_tools():
    events = []
    # tool storm with sparse chat
    for i in range(80):
        events.append(_ev("tool_call", tool_id=f"t{i}"))
        events.append(_ev("tool_call_update", tool_id=f"t{i}"))
        events.append(_ev("tool_call_update", tool_id=f"t{i}"))
        if i % 2 == 0:
            events.append(_ev("user_message_chunk", f"user {i}"))
            events.append(_ev("agent_message_chunk", f"agent {i}"))
        events.append(_ev("task_completed"))
    out = select_tip_paint_lines(events, 50)
    d = sum(1 for e in out if is_dialogue_speech(e))
    assert d == 50, d
    types = [
        (e.get("params") or {}).get("update", {}).get("sessionUpdate") for e in out
    ]
    assert "task_completed" not in types
    tools = sum(1 for t in types if t in ("tool_call", "tool_call_update"))
    assert tools <= 50, tools
    assert len(out) <= 100, len(out)


def test_recent_user_not_pushed_out_by_tools():
    events = []
    for i in range(30):
        events.append(_ev("agent_message_chunk", f"old agent {i}"))
        events.append(_ev("tool_call", tool_id=f"old{i}"))
        events.append(_ev("tool_call_update", tool_id=f"old{i}"))
    events.append(_ev("user_message_chunk", "ERIC_LATEST hello"))
    events.append(_ev("agent_message_chunk", "AGENT_LATEST reply"))
    for i in range(20):
        events.append(_ev("tool_call", tool_id=f"new{i}"))
        events.append(_ev("tool_call_update", tool_id=f"new{i}"))
    out = select_tip_paint_lines(events, 10)
    texts = []
    for e in out:
        c = (e.get("params") or {}).get("update", {}).get("content") or {}
        if isinstance(c, dict) and c.get("text"):
            texts.append(c["text"])
    blob = " ".join(texts)
    assert "ERIC_LATEST" in blob, texts
    assert "AGENT_LATEST" in blob, texts


def test_tripg_hot_tip_includes_recent_dialogue():
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
    events = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        ev = line_to_tui_event(line, "hot")
        if ev:
            events.append(ev)
    out = select_tip_paint_lines(events, 50)
    d = sum(1 for e in out if is_dialogue_speech(e))
    assert d == 50, d
    # last dialogue in tip should be near file end — include recent restart msg if present
    texts = []
    for e in out:
        c = (e.get("params") or {}).get("update", {}).get("content") or {}
        if isinstance(c, dict) and c.get("text"):
            texts.append(str(c["text"])[:100])
    # at least some agent text in the last third of tip
    assert any(t.strip() for t in texts[-15:]), "tip tail empty of speech"
