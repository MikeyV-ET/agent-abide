"""GrokBackend owns native→aa.stream→hot ingest."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

pytest.importorskip("zstandard")

from grok_backend import GrokBackend  # noqa: E402


def _frame(text: str, su: str = "agent_message_chunk") -> dict:
    return {
        "timestamp": 1700000000.0,
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": su,
                "content": {"type": "text", "text": text},
            }
        },
    }


def test_sync_hot_stream_from_backend(tmp_path: Path):
    home = tmp_path / "TripG"
    (home / "asdaaas").mkdir(parents=True)
    sess = tmp_path / "session"
    sess.mkdir()
    updates = sess / "updates.jsonl"
    lines = [_frame("hello"), _frame(" world")]
    updates.write_text("".join(json.dumps(x) + "\n" for x in lines))

    b = GrokBackend()
    b._session_id = "sid-test"
    b._session_dir = sess
    b.configure_aa_history(home, "TripG", enabled=True)

    r = b.sync_hot_stream()
    assert r.get("status") == "ok", r
    assert r.get("lines_ingested") == 2
    hot = home / "asdaaas" / "history" / "hot.jsonl"
    assert hot.exists()
    evs = [json.loads(l) for l in hot.read_text().splitlines() if l.strip()]
    assert len(evs) == 2
    assert evs[0]["backend"] == "grok"
    assert evs[0]["format"] == "aa.stream"
    assert evs[0]["body"]["text"] == "hello"

    r2 = b.sync_hot_stream()
    assert r2.get("lines_ingested") == 0


def test_sync_skipped_without_configure():
    b = GrokBackend()
    r = b.sync_hot_stream()
    assert r["status"] == "skipped"


def test_process_frames_triggers_sync(tmp_path: Path):
    home = tmp_path / "A"
    (home / "asdaaas").mkdir(parents=True)
    sess = tmp_path / "s"
    sess.mkdir()
    updates = sess / "updates.jsonl"
    # pre-write so tail can find bytes; process_frames will also sync
    fr = _frame("x")
    updates.write_text(json.dumps(fr) + "\n")

    b = GrokBackend()
    b._session_id = "s1"
    b._session_dir = sess
    b.configure_aa_history(home, "A", enabled=True)
    b._process_update_frames(
        [fr], [], [], set(), None, None, None,
    )
    hot = home / "asdaaas" / "history" / "hot.jsonl"
    assert hot.exists() and hot.stat().st_size > 0
