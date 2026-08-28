"""ChatState.prune_items — long-run memory bound for dual-path model."""
import sys
from pathlib import Path

TUI = Path(__file__).resolve().parents[1] / "tui"
sys.path.insert(0, str(TUI))

from chat_model import ChatState, apply_event, prune_items, SpeechItem  # noqa: E402


def _ev(su, **kwargs):
    return {"timestamp": 1, "params": {"update": {"sessionUpdate": su, **kwargs}}}


def test_prune_keeps_recent():
    st = ChatState()
    for i in range(600):
        apply_event(st, _ev("user_message_chunk", content={"text": f"u{i}"}))
    n = prune_items(st, max_items=500)
    assert n > 0
    assert len(st.items) == 500
    assert isinstance(st.items[-1], SpeechItem)
    assert st.items[-1].text == "u599"


def test_prune_rebuilds_tool_map():
    st = ChatState()
    for i in range(10):
        apply_event(st, _ev("tool_call", toolCallId=f"t{i}", title=f"T{i}"))
    prune_items(st, max_items=5)
    assert "t9" in st.tools
    assert "t0" not in st.tools
    apply_event(
        st,
        _ev(
            "tool_call_update",
            toolCallId="t9",
            status="completed",
            content=[{"type": "content", "content": {"text": "done"}}],
        ),
    )
    assert st.items[st.tools["t9"]].output == "done"


def test_open_stream_survives_prune():
    st = ChatState()
    for i in range(100):
        apply_event(st, _ev("user_message_chunk", content={"text": f"u{i}"}))
    apply_event(st, _ev("agent_message_chunk", content={"text": "hello"}))
    prune_items(st, max_items=50)
    assert st.open_speech_idx is not None
    apply_event(st, _ev("agent_message_chunk", content={"text": " world"}))
    assert st.items[st.open_speech_idx].text == "hello world"
