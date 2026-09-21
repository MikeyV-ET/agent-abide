"""-t N is dialogue budget; tip collapses tools and drops session meta."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import thin_tip_events, is_dialogue_speech  # noqa: E402


def _ev(su: str, text: str = "", tool_id: str = "") -> dict:
    u = {"sessionUpdate": su}
    if text:
        u["content"] = {"text": text}
    if tool_id:
        u["toolCallId"] = tool_id
        u["status"] = "completed" if su == "tool_call_update" else "running"
        u["title"] = "run"
    return {"params": {"update": u}}


def test_thin_tip_respects_dialogue_n_and_collapses_tools():
    events = []
    # older noise
    for i in range(20):
        events.append(_ev("user_message_chunk", f"old {i}"))
        events.append(_ev("tool_call", tool_id=f"t{i}"))
        events.append(_ev("tool_call_update", tool_id=f"t{i}"))
        events.append(_ev("task_completed"))
        events.append(_ev("turn_completed"))
    # tip region: 5 dialogue + multi tool frames + meta
    for i in range(5):
        events.append(_ev("user_message_chunk", f"tip user {i}"))
        events.append(_ev("agent_message_chunk", f"tip agent {i}"))
        events.append(_ev("tool_call", tool_id=f"tip{i}"))
        events.append(_ev("tool_call_update", tool_id=f"tip{i}"))
        events.append(_ev("tool_call_update", tool_id=f"tip{i}"))  # third state
        events.append(_ev("background_tasks"))
        events.append(_ev("task_completed"))

    out = thin_tip_events(events, 10)  # 10 dialogue
    dialogue = sum(1 for e in out if is_dialogue_speech(e))
    assert dialogue == 10, dialogue
    types = [
        (e.get("params") or {}).get("update", {}).get("sessionUpdate") for e in out
    ]
    assert "task_completed" not in types
    assert "background_tasks" not in types
    assert "turn_completed" not in types
    # one entry per tip tool id (last update wins) — 5 tools in tip span
    toolish = [t for t in types if t in ("tool_call", "tool_call_update")]
    assert len(toolish) == 5, toolish
    assert len(out) < 40  # was 5*(2 speech + 3 tool + 2 meta) = 35+ without thin


def test_live_tripg_hot_t50_paint_bound():
    """Regression against real Trip-G density: -t50 must not yield 300+ paint."""
    hot = Path("/home/eric/agents/Trip-G/asdaaas/history/hot.jsonl")
    if not hot.exists():
        return
    from tui_history import line_to_tui_event, is_dialogue_speech, is_speech_tui_event

    size = hot.stat().st_size
    read_size = min(size, 8 * 1024 * 1024)
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
    out = thin_tip_events(events, 50)
    d = sum(1 for e in out if is_dialogue_speech(e))
    s = sum(1 for e in out if is_speech_tui_event(e))
    assert d == 50, d
    assert len(out) < 150, f"still too fat: paint={len(out)} speech={s} dialogue={d}"
