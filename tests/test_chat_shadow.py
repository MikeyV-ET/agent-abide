"""Shadow-validate ChatState against realistic event streams."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))

from chat_model import (
    ChatState, apply_events, SpeechItem, ThinkingItem, ToolItem, TurnMark,
)
from event_coalesce import coalesce_events


def _ev(su, **kw):
    return {"params": {"update": {"sessionUpdate": su, **kw}}, "timestamp": 1}


def test_shadow_busy_turn_like_squiggy():
    """Verbose tool storm + thinking + speech — model stays coherent."""
    raw = []
    raw.append(_ev("user_message_chunk", content={"text": "please investigate"}))
    for i in range(5):
        raw.append(_ev("agent_thought_chunk", content={"text": f"step{i} "}))
    raw.append(_ev("agent_message_chunk", content={"text": "I will look."}))
    raw.append(_ev("tool_call", toolCallId="bash1", title="run_terminal_command"))
    for i in range(12):
        raw.append(_ev(
            "tool_call_update", toolCallId="bash1", status="in_progress",
            content=[{"type": "content", "content": {"text": f"out line {i}\n" * 3}}],
        ))
    raw.append(_ev(
        "tool_call_update", toolCallId="bash1", status="completed",
        content=[{"type": "content", "content": {"text": "final\n" * 50}}],
    ))
    raw.append(_ev("agent_message_chunk", content={"text": "Done."}))

    batch = coalesce_events(raw)
    # tool updates should collapse a lot
    assert len(batch) < len(raw)

    s = ChatState()
    apply_events(s, batch)

    thinks = [i for i in s.items if isinstance(i, ThinkingItem)]
    assert len(thinks) == 1
    assert "step0" in thinks[0].text and "step4" in thinks[0].text

    tools = [i for i in s.items if isinstance(i, ToolItem)]
    assert len(tools) == 1
    assert tools[0].status == "completed"
    assert tools[0].collapsed is True
    assert "final" in tools[0].output

    speeches = [i for i in s.items if isinstance(i, SpeechItem) and i.kind == "agent"]
    assert any("Done" in i.text for i in speeches)
    assert s.logical_turn == 1
    assert any(isinstance(i, TurnMark) for i in s.items)


def test_shadow_multi_tool():
    raw = [
        _ev("tool_call", toolCallId="a", title="read"),
        _ev("tool_call_update", toolCallId="a", status="completed",
            content=[{"type": "content", "content": {"text": "file a"}}]),
        _ev("tool_call", toolCallId="b", title="exec"),
        _ev("tool_call_update", toolCallId="b", status="failed",
            content=[{"type": "content", "content": {"text": "err"}}]),
    ]
    s = ChatState()
    apply_events(s, coalesce_events(raw))
    tools = [i for i in s.items if isinstance(i, ToolItem)]
    assert len(tools) == 2
    assert tools[0].status == "completed" and tools[1].status == "failed"
