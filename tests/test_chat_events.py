import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))
from chat_events import session_update, chunk_text, tool_call_id, is_stream_chunk

def test_session_update():
    e = {"params": {"update": {"sessionUpdate": "tool_call", "toolCallId": "x"}}}
    assert session_update(e) == "tool_call"
    assert tool_call_id(e) == "x"
    assert not is_stream_chunk(e)

def test_chunk_text():
    e = {"params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}}}}
    assert chunk_text(e) == "hi"
    assert is_stream_chunk(e)
