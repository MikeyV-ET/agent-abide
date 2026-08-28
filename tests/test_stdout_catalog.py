"""Stdout wire catalog: atomic exemplars + timeline refs."""
import json
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "core"
sys.path.insert(0, str(CORE))

from stdout_catalog import catalog_key, StdoutWireRecorder  # noqa: E402


def _su(session_update: str, text: str) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "abc",
            "update": {
                "sessionUpdate": session_update,
                "content": {"text": text},
            },
        },
    })


def test_high_churn_chunks_share_key():
    a = _su("agent_message_chunk", "hello")
    b = _su("agent_message_chunk", "hello world much longer")
    assert catalog_key(a) == catalog_key(b) == "su:agent_message_chunk"


def test_different_su_types_differ():
    a = _su("agent_message_chunk", "x")
    b = _su("tool_call", "x")
    assert catalog_key(a) != catalog_key(b)


def test_recorder_dedupes_timeline(tmp_path):
    rec = StdoutWireRecorder(tmp_path)
    assert rec.active
    id1 = rec.record(1.0, _su("agent_message_chunk", "aaa"))
    id2 = rec.record(2.0, _su("agent_message_chunk", "bbb different"))
    id3 = rec.record(3.0, _su("tool_call", "t"))
    assert id1 == id2  # same atomic type
    assert id3 != id1
    rec.close()

    timeline = [json.loads(l) for l in (tmp_path / "stdout_log.jsonl").read_text().splitlines()]
    catalog = [json.loads(l) for l in (tmp_path / "stdout_catalog.jsonl").read_text().splitlines()]
    assert len(timeline) == 3
    assert [e["ref"] for e in timeline] == [id1, id1, id3]
    assert all("ts" in e and "ref" in e for e in timeline)
    assert len(catalog) == 2
    # First-seen exemplar retained (aaa, not bbb)
    by_id = {c["id"]: c for c in catalog}
    assert "aaa" in by_id[id1]["example"]
    assert "bbb" not in by_id[id1]["example"]


def test_exact_repeat_notification(tmp_path):
    raw = json.dumps({
        "jsonrpc": "2.0",
        "method": "_x.ai/queue/changed",
        "params": {"n": 1},
    })
    rec = StdoutWireRecorder(tmp_path)
    a = rec.record(1.0, raw)
    b = rec.record(2.0, raw)
    assert a == b
    rec.close()
    catalog = (tmp_path / "stdout_catalog.jsonl").read_text().strip().splitlines()
    timeline = (tmp_path / "stdout_log.jsonl").read_text().strip().splitlines()
    assert len(catalog) == 1
    assert len(timeline) == 2


def test_reload_preserves_ids(tmp_path):
    rec = StdoutWireRecorder(tmp_path)
    cid = rec.record(1.0, _su("agent_message_chunk", "first"))
    rec.close()
    rec2 = StdoutWireRecorder(tmp_path)
    cid2 = rec2.record(2.0, _su("agent_message_chunk", "second"))
    assert cid2 == cid
    rec2.close()
    catalog = (tmp_path / "stdout_catalog.jsonl").read_text().strip().splitlines()
    assert len(catalog) == 1  # no duplicate catalog row
