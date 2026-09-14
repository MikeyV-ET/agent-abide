"""Per-agent history tail hook."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from full_stream_hook import load_full_stream_config, maybe_tail_grok_after_turn  # noqa: E402
from aa_stream import hot_path, agent_full_stream_dir  # noqa: E402
from full_stream import agent_full_stream_dir as afs  # noqa: E402


def test_skip_without_config(tmp_path, monkeypatch):
    home = tmp_path / "AgentZ"
    (home / "asdaaas").mkdir(parents=True)
    # patch agent_dir
    def fake_agent_dir(name, env=None):
        return home / "asdaaas"
    import full_stream_hook as h
    monkeypatch.setattr(h, "_agent_home", lambda name, env=None: home)
    assert maybe_tail_grok_after_turn("AgentZ") is None


def test_tail_when_enabled(tmp_path, monkeypatch):
    home = tmp_path / "AgentZ"
    fs = home / "asdaaas" / "history"
    fs.mkdir(parents=True)
    (fs / "config.json").write_text(json.dumps({"tail_grok": True}))
    upd = tmp_path / "updates.jsonl"
    upd.write_text(
        json.dumps(
            {
                "timestamp": 1700000001.0,
                "params": {
                    "update": {"sessionUpdate": "agent_message_chunk", "content": "x"}
                },
            }
        )
        + "\n"
    )
    import full_stream_hook as h
    monkeypatch.setattr(h, "_agent_home", lambda name, env=None: home)

    def fake_tail(agent_home, agent, **kw):
        return {"status": "ok", "lines_ingested": 1, "hot_bytes": 10}

    monkeypatch.setattr("aa_stream.tail_grok_once", fake_tail)
    # import inside function uses aa_stream.tail_grok_once - need to patch before call
    import aa_stream
    monkeypatch.setattr(aa_stream, "tail_grok_once", fake_tail)
    r = maybe_tail_grok_after_turn("AgentZ")
    assert r and r["lines_ingested"] == 1
