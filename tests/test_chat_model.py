"""Pure chat model / event reducer tests."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))

from chat_model import (
    ChatState, apply_event, apply_events, SpeechItem, ThinkingItem, ToolItem,
    tool_snippet, extract_interjections,
)
from event_coalesce import coalesce_events


def _ev(su, **update):
    u = {"sessionUpdate": su, **update}
    return {"params": {"update": u}, "timestamp": 1}


def test_message_stream():
    s = ChatState()
    apply_event(s, _ev("agent_message_chunk", content={"text": "Hello"}))
    apply_event(s, _ev("agent_message_chunk", content={"text": " world"}))
    assert len(s.items) == 1
    assert isinstance(s.items[0], SpeechItem)
    assert s.items[0].text == "Hello world"


def test_thinking_priority_stream():
    s = ChatState()
    apply_event(s, _ev("agent_thought_chunk", content={"text": "I wonder"}))
    apply_event(s, _ev("agent_thought_chunk", content={"text": " if"}))
    assert len(s.items) == 1
    assert isinstance(s.items[0], ThinkingItem)
    assert s.items[0].text == "I wonder if"


def test_tool_default_collapsed_and_snippet():
    s = ChatState()
    apply_event(s, _ev("tool_call", toolCallId="t1", title="bash"))
    body = "\n".join(f"line{i}" for i in range(20))
    apply_event(s, _ev(
        "tool_call_update", toolCallId="t1", status="completed",
        content=[{"type": "content", "content": {"text": body}}],
    ))
    tool = s.items[0]
    assert isinstance(tool, ToolItem)
    assert tool.collapsed is True
    assert tool.status == "completed"
    snip = tool_snippet(tool, 4)
    assert snip.count("\n") == 3
    assert "line0" in snip and "line19" not in snip


def test_tool_latest_wins_with_coalesce():
    events = [
        _ev("tool_call", toolCallId="t1", title="x"),
        _ev("tool_call_update", toolCallId="t1",
            content=[{"type": "content", "content": {"text": "a"}}]),
        _ev("tool_call_update", toolCallId="t1",
            content=[{"type": "content", "content": {"text": "b"}}]),
    ]
    batch = coalesce_events(events)
    s = ChatState()
    apply_events(s, batch)
    assert s.items[-1].output == "b"


def test_user_turn_increments():
    s = ChatState()
    apply_event(s, _ev("user_message_chunk", content={"text": "hi"}))
    assert s.logical_turn == 1
    assert any(isinstance(i, SpeechItem) and i.kind == "user" for i in s.items)


def test_interjection_extract():
    clean, msgs = extract_interjections("pre\n<interjection>\n[system: x]\nhi\n</interjection>\npost")
    assert "hi" in msgs[0]
    assert "pre" in clean and "post" in clean
    assert "<interjection>" not in clean


def test_boundary_tool_closes_speech():
    s = ChatState()
    apply_event(s, _ev("agent_message_chunk", content={"text": "A"}))
    apply_event(s, _ev("tool_call", toolCallId="t", title="r"))
    apply_event(s, _ev("agent_message_chunk", content={"text": "B"}))
    speeches = [i for i in s.items if isinstance(i, SpeechItem)]
    assert len(speeches) == 2
    assert speeches[0].text == "A" and speeches[1].text == "B"
