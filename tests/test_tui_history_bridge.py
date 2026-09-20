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
