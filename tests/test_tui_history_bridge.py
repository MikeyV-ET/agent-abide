"""tui_history aa.stream → TUI update bridge."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from tui_history import aa_event_to_tui_update, resolve_history_source  # noqa: E402


def test_text_delta_assistant():
    ev = {
        "format": "aa.stream",
        "v": 1,
        "class": "message",
        "role": "assistant",
        "ts": 1700000000.0,
        "body": {"kind": "text_delta", "text": "hi"},
    }
    u = aa_event_to_tui_update(ev)
    assert u is not None
    assert u["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
    assert u["params"]["update"]["content"]["text"] == "hi"


def test_user_text():
    ev = {
        "format": "aa.stream",
        "role": "user",
        "body": {"kind": "text", "text": "yo"},
    }
    u = aa_event_to_tui_update(ev)
    assert u["params"]["update"]["sessionUpdate"] == "user_message_chunk"


def test_meta_skipped():
    ev = {"format": "aa.stream", "body": {"kind": "meta", "label": "attachment"}}
    assert aa_event_to_tui_update(ev) is None


def test_resolve_prefers_hot(tmp_path: Path):
    home = tmp_path / "ag"
    hist = home / "asdaaas" / "history"
    hist.mkdir(parents=True)
    hot = hist / "hot.jsonl"
    hot.write_text('{"format":"aa.stream","body":{"kind":"text","text":"a"}}\n')
    kind, path = resolve_history_source(home, prefer="auto")
    assert kind == "hot"
    assert path == hot


def test_select_tail_speech_first():
    from tui_history import select_tail_events, is_speech_tui_event

    def msg(role, text):
        su = "user_message_chunk" if role == "user" else "agent_message_chunk"
        return {
            "params": {"update": {"sessionUpdate": su, "content": {"text": text}}},
        }

    def tool(i):
        return {
            "params": {"update": {"sessionUpdate": "tool_call", "toolCallId": str(i), "title": "t"}},
        }

    evs = [tool(i) for i in range(10)]
    for i in range(5):
        evs.append(msg("user", f"u{i}"))
        evs.append(tool(100 + i))
        evs.append(msg("assistant", f"a{i}"))
    sel = select_tail_events(evs, 3, speech_first=True)
    speech = [e for e in sel if is_speech_tui_event(e)]
    assert len(speech) == 3
    texts = [e["params"]["update"]["content"]["text"] for e in speech]
    # last 3 speech in stream: ... u3, a3, u4, a4 → last 3 are a3, u4, a4
    assert texts == ["a3", "u4", "a4"]
    # tools between them retained
    assert any(e["params"]["update"]["sessionUpdate"] == "tool_call" for e in sel)


def test_line_to_tui_hot_and_updates():
    from tui_history import line_to_tui_event
    import json
    hot = {
        "format": "aa.stream",
        "role": "assistant",
        "body": {"kind": "text", "text": "hello"},
    }
    ev = line_to_tui_event(json.dumps(hot), "hot")
    assert ev["params"]["update"]["sessionUpdate"] == "agent_message_chunk"
    grok = {
        "params": {
            "update": {
                "sessionUpdate": "user_message_chunk",
                "content": {"text": "hi"},
            }
        }
    }
    ev2 = line_to_tui_event(json.dumps(grok), "updates")
    assert ev2["params"]["update"]["content"]["text"] == "hi"


def test_grok_native_passthrough_preserves_interjection():
    import json
    from tui_history import aa_event_to_tui_update

    bash = "x\n<interjection>\n[eric] hi there</interjection>\ny\n"
    native = {
        "timestamp": 42.0,
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc1",
                "status": "in_progress",
                "content": [
                    {"type": "content", "content": {"type": "text", "text": bash}}
                ],
            }
        },
    }
    ev = {
        "format": "aa.stream",
        "backend": "grok",
        "native": {"event": native},
        "body": {"kind": "tool_call"},  # lossy body — must not win
    }
    out = aa_event_to_tui_update(ev)
    assert out["params"]["update"]["sessionUpdate"] == "tool_call_update"
    assert "hi there" in out["params"]["update"]["content"][0]["content"]["text"]


def test_tool_result_decodes_bash_bytes():
    import json
    from tui_history import aa_event_to_tui_update

    text = "<interjection>\nbell</interjection>"
    payload = {"type": "Bash", "output": list(text.encode())}
    ev = {
        "format": "aa.stream",
        "body": {
            "kind": "tool_result",
            "tool_id": "t",
            "content": json.dumps(payload),
            "status": "completed",
        },
    }
    out = aa_event_to_tui_update(ev)
    blob = out["params"]["update"]["content"][0]["content"]["text"]
    assert "<interjection>" in blob
    assert "bell" in blob


def test_tui_content_text_shapes():
    from tui_history import tui_content_text
    assert tui_content_text({"content": {"text": "a"}}) == "a"
    assert tui_content_text({"content": "plain"}) == "plain"
    assert tui_content_text({"content": None}) == ""
    assert tui_content_text({"content": [{"type": "text", "text": "x"}, {"content": {"text": "y"}}]}) == "xy"
    assert tui_content_text("nope") == ""


def test_interjection_kind_becomes_tool_update_for_block():
    from tui_history import aa_event_to_tui_update
    ev = {
        "format": "aa.stream",
        "role": "user",
        "body": {"kind": "interjection", "text": "[eric (via tui)] hi mid-turn"},
    }
    u = aa_event_to_tui_update(ev)
    assert u is not None
    assert u["params"]["update"]["sessionUpdate"] == "tool_call_update"
    blob = u["params"]["update"]["content"][0]["content"]["text"]
    assert "<interjection>" in blob
    assert "hi mid-turn" in blob
