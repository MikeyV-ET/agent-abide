"""aa.stream hot tip + grok tailer tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

zstd = pytest.importorskip("zstandard")

from aa_stream import (  # noqa: E402
    FORMAT_V,
    assert_aa_hot_path,
    ensure_aa_stream_layout,
    hot_path,
    is_backend_native_path,
    map_grok_event,
    read_hot_meta,
    tail_grok_once,
    wrap_grok_line,
)
from full_stream import agent_full_stream_dir, prune_hot  # noqa: E402


def test_map_grok_message_chunk():
    obj = {
        "timestamp": 1700000000.0,
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": "hello",
            }
        },
    }
    c, phase, role, body = map_grok_event(obj)
    assert c == "message"
    assert phase == "delta"
    assert role == "assistant"
    assert body["kind"] == "text_delta"
    assert body["text"] == "hello"


def test_wrap_has_required_spine():
    obj = {
        "timestamp": 1700000000.5,
        "params": {"update": {"sessionUpdate": "user_message_chunk", "content": "hi"}},
    }
    ev = wrap_grok_line(
        obj, agent="A", session_id="sid", stream_seq=0, source_path="/x", offset=0
    )
    assert ev["v"] == FORMAT_V
    assert ev["format"] == "aa.stream"
    assert ev["backend"] == "grok"
    assert ev["native"]["schema"] == "grok.session_update.v1"
    assert ev["native"]["event"]["timestamp"] == 1700000000.5
    assert ev["body"]["text"] == "hi"


def test_backend_native_guard():
    assert is_backend_native_path(Path("/home/eric/.grok/sessions/foo/updates.jsonl"))
    assert is_backend_native_path(Path("/home/eric/.codex/sessions/2026/09/11/rollout.jsonl"))
    assert is_backend_native_path(Path("/home/eric/.claude/projects/-x/sid.jsonl"))
    assert not is_backend_native_path(Path("/home/eric/agents/X/asdaaas/history/hot.jsonl"))
    with pytest.raises(ValueError):
        assert_aa_hot_path(Path("/home/eric/.grok/sessions/a/b/updates.jsonl"))


def test_prune_refuses_backend(tmp_path: Path):
    # fake backend path under tmp by name trick — use real structure
    backend = tmp_path / ".grok" / "sessions" / "x" / "updates.jsonl"
    backend.parent.mkdir(parents=True)
    backend.write_text("x\n" * 100)
    fs = tmp_path / "agent" / "asdaaas" / "history"
    fs.mkdir(parents=True)
    # path must contain /.grok/sessions/
    # tmp_path doesn't; construct with marker in string via symlink?
    # Direct unit: prune_hot checks resolved string for marker
    # Create path that includes the marker
    root = tmp_path / "home" / ".grok" / "sessions" / "enc" / "uuid"
    root.mkdir(parents=True)
    hot = root / "updates.jsonl"
    # make large enough
    line = json.dumps({"timestamp": 1.0, "params": {"update": {"sessionUpdate": "x"}}}) + "\n"
    hot.write_text(line * 5000)
    fs = tmp_path / "fs"
    fs.mkdir()
    # path will be .../.grok/sessions/...
    with pytest.raises(ValueError, match="backend-native"):
        prune_hot(hot, fs, apply=True, max_bytes=100, keep_bytes=50)


def test_tail_grok_once(tmp_path: Path):
    home = tmp_path / "TripTest"
    (home / "asdaaas").mkdir(parents=True)
    # fake grok updates
    upd = tmp_path / "updates.jsonl"
    lines = []
    for i in range(5):
        lines.append(
            json.dumps(
                {
                    "timestamp": 1700000000.0 + i,
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": f"c{i}",
                        }
                    },
                }
            )
        )
    upd.write_text("\n".join(lines) + "\n")

    r1 = tail_grok_once(home, "TripTest", source=upd, session_id="sessA")
    assert r1["status"] == "ok"
    assert r1["lines_ingested"] == 5
    fs = agent_full_stream_dir(home)
    hp = hot_path(fs)
    hot_lines = hp.read_text().strip().splitlines()
    assert len(hot_lines) == 5
    ev0 = json.loads(hot_lines[0])
    assert ev0["v"] == 1
    assert ev0["stream_seq"] == 0
    assert ev0["body"]["text"] == "c0"
    assert ev0["native"]["event"]["params"]["update"]["content"] == "c0"
    meta = read_hot_meta(fs)
    assert meta["stream_seq_next"] == 5
    assert meta["backends"]["grok"]["byte_offset"] == upd.stat().st_size

    # second tail: no new lines
    r2 = tail_grok_once(home, "TripTest", source=upd, session_id="sessA")
    assert r2["lines_ingested"] == 0
    assert len(hp.read_text().strip().splitlines()) == 5

    # append source
    with upd.open("a") as f:
        f.write(
            json.dumps(
                {
                    "timestamp": 1700000009.0,
                    "params": {
                        "update": {"sessionUpdate": "user_message_chunk", "content": "new"}
                    },
                }
            )
            + "\n"
        )
    r3 = tail_grok_once(home, "TripTest", source=upd, session_id="sessA")
    assert r3["lines_ingested"] == 1
    assert json.loads(hp.read_text().strip().splitlines()[-1])["body"]["text"] == "new"
