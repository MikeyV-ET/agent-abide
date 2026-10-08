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
    append_hot_events,
    assert_aa_hot_path,
    check_history_continuity,
    check_hot_ts_monotonic,
    default_hot_meta,
    ensure_aa_stream_layout,
    hot_path,
    is_backend_native_path,
    map_grok_event,
    maybe_prune_hot,
    read_hot_meta,
    tail_grok_once,
    wrap_grok_line,
    write_hot_meta,
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


def test_check_hot_ts_monotonic_ok_and_equal(tmp_path: Path):
    home = tmp_path / "A"
    (home / "asdaaas").mkdir(parents=True)
    fs = ensure_aa_stream_layout(agent_full_stream_dir(home), "A")
    events = [
        {"v": 1, "ts": 100.0, "stream_seq": 0, "session_id": "s"},
        {"v": 1, "ts": 100.0, "stream_seq": 1, "session_id": "s"},
        {"v": 1, "ts": 101.0, "stream_seq": 2, "session_id": "s"},
    ]
    append_hot_events(fs, events)
    r = check_hot_ts_monotonic(fs)
    assert r["status"] == "ok"
    assert r["inversions"] == []


def test_check_hot_ts_tiebreak_session_id(tmp_path: Path):
    home = tmp_path / "A"
    (home / "asdaaas").mkdir(parents=True)
    fs = ensure_aa_stream_layout(agent_full_stream_dir(home), "A")
    hp = hot_path(fs)
    hp.write_text(
        '{"v":1,"ts":100.0,"session_id":"b","stream_seq":0}\n'
        '{"v":1,"ts":100.0,"session_id":"a","stream_seq":1}\n'
    )
    r = check_hot_ts_monotonic(fs)
    assert r["status"] == "error"
    assert r["inversions"][0]["session_id"] == "a"


def test_check_hot_ts_monotonic_detects_backward(tmp_path: Path):
    home = tmp_path / "A"
    (home / "asdaaas").mkdir(parents=True)
    fs = ensure_aa_stream_layout(agent_full_stream_dir(home), "A")
    hp = hot_path(fs)
    hp.write_text(
        '{"v":1,"ts":200.0,"stream_seq":0}\n'
        '{"v":1,"ts":50.0,"stream_seq":1}\n'
    )
    r = check_hot_ts_monotonic(fs)
    assert r["status"] == "error"
    assert len(r["inversions"]) == 1
    assert r["inversions"][0]["stream_seq"] == 1
    cont = json.loads((fs / "continuity.json").read_text())
    assert cont["status"] == "error"


def test_check_history_continuity_ok(tmp_path: Path):
    home = tmp_path / "A"
    (home / "asdaaas").mkdir(parents=True)
    fs = ensure_aa_stream_layout(agent_full_stream_dir(home), "A")
    append_hot_events(fs, [{"v": 1, "ts": 1.0, "stream_seq": 0}])
    r = check_history_continuity(fs)
    assert r["status"] == "ok"
    assert r["chunk_inversions"] == []


def test_append_refuses_ts_behind_tip(tmp_path: Path):
    home = tmp_path / "A"
    (home / "asdaaas").mkdir(parents=True)
    fs = ensure_aa_stream_layout(agent_full_stream_dir(home), "A")
    append_hot_events(fs, [{"v": 1, "ts": 500.0, "stream_seq": 0, "session_id": "s"}])
    n = append_hot_events(
        fs,
        [
            {"v": 1, "ts": 100.0, "stream_seq": 1, "session_id": "s"},
            {"v": 1, "ts": 600.0, "stream_seq": 2, "session_id": "s"},
        ],
    )
    assert n > 0
    lines = hot_path(fs).read_text().strip().splitlines()
    ts = [json.loads(l)["ts"] for l in lines]
    assert ts == [500.0, 600.0]


def test_maybe_prune_hot_skips_under_cap(tmp_path: Path):
    home = tmp_path / "A"
    (home / "asdaaas").mkdir(parents=True)
    fs = ensure_aa_stream_layout(agent_full_stream_dir(home), "A")
    hot = hot_path(fs)
    hot.write_text('{"v":1,"session_id":"s"}\n')
    assert maybe_prune_hot(fs, agent="A") is None
    assert hot.stat().st_size > 0


def test_append_hot_events_prunes_when_over_cap(tmp_path: Path):
    home = tmp_path / "A"
    (home / "asdaaas").mkdir(parents=True)
    fs = ensure_aa_stream_layout(agent_full_stream_dir(home), "A")
    line = json.dumps(
        {
            "v": 1,
            "format": "aa.stream",
            "session_id": "s",
            "ts": 1700000000.0,
            "stream_seq": 0,
            "body": {"kind": "text", "text": "x" * 200},
        }
    ) + "\n"
    # Build a hot file over a tiny cap, then one append should seal+trim.
    hot = hot_path(fs)
    hot.write_text(line * 40)
    size = hot.stat().st_size
    meta = default_hot_meta("A", fs)
    meta["policy"] = {"max_bytes": size // 2, "keep_bytes": max(size // 6, 80)}
    write_hot_meta(fs, meta)
    n = append_hot_events(
        fs,
        [
            {
                "v": 1,
                "format": "aa.stream",
                "session_id": "s",
                "ts": 1700000100.0,
                "stream_seq": 40,
                "body": {"kind": "text", "text": "tip"},
            }
        ],
    )
    assert n > 0
    assert hot.stat().st_size < size
    assert hot.stat().st_size <= meta["policy"]["keep_bytes"] + 2000
    chunks = list((fs / "chunks").glob("*.jsonl.zst"))
    assert len(chunks) >= 1
    man = (fs / "manifest.jsonl").read_text().strip()
    assert man


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
