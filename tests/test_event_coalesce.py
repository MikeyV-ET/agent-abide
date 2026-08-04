"""Unit tests for tui/event_coalesce.py"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))

from event_coalesce import coalesce_events


def _msg(text, su="agent_message_chunk"):
    return {"params": {"update": {"sessionUpdate": su, "content": {"text": text}}}}


def _tool_upd(tid, text, status="in_progress"):
    return {
        "params": {
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tid,
                "status": status,
                "content": [{"type": "content", "content": {"text": text}}],
            }
        }
    }


def _tool_call(tid, title="run"):
    return {
        "params": {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": tid,
                "title": title,
            }
        }
    }


def test_empty():
    assert coalesce_events([]) == []


def test_merge_message_chunks():
    out = coalesce_events([_msg("a"), _msg("b"), _msg("c")])
    assert len(out) == 1
    assert out[0]["params"]["update"]["content"]["text"] == "abc"


def test_merge_thought_chunks_separate_from_message():
    out = coalesce_events([_msg("a"), _msg("x", "agent_thought_chunk"), _msg("y", "agent_thought_chunk")])
    assert len(out) == 2
    assert out[0]["params"]["update"]["content"]["text"] == "a"
    assert out[1]["params"]["update"]["content"]["text"] == "xy"


def test_tool_update_latest_wins():
    out = coalesce_events([
        _tool_upd("t1", "one"),
        _tool_upd("t1", "two"),
        _tool_upd("t1", "three"),
    ])
    assert len(out) == 1
    body = out[0]["params"]["update"]["content"][0]["content"]["text"]
    assert body == "three"


def test_interleaved_tools_and_messages():
    out = coalesce_events([
        _tool_call("t1"),
        _tool_upd("t1", "a"),
        _msg("hi"),
        _msg("!"),
        _tool_upd("t1", "b"),
    ])
    # tool_call, one tool_upd (latest b replaces a), one merged msg
    assert len(out) == 3
    assert out[0]["params"]["update"]["sessionUpdate"] == "tool_call"
    assert out[1]["params"]["update"]["content"][0]["content"]["text"] == "b"
    assert out[2]["params"]["update"]["content"]["text"] == "hi!"


def test_different_tool_ids_kept():
    out = coalesce_events([_tool_upd("a", "1"), _tool_upd("b", "2"), _tool_upd("a", "3")])
    assert len(out) == 2
    # a latest is 3, b is 2
    texts = {
        e["params"]["update"]["toolCallId"]: e["params"]["update"]["content"][0]["content"]["text"]
        for e in out
    }
    assert texts["a"] == "3"
    assert texts["b"] == "2"
