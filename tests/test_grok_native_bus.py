"""Phase 2: one updates cursor fans out to hot + collect."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

pytest.importorskip("zstandard")

from grok_native_bus import GrokNativeBus  # noqa: E402
from grok_backend import FileEventSource  # noqa: E402


def _line(su: str, **extra) -> str:
    return json.dumps(
        {
            "timestamp": time.time(),
            "method": "session/update",
            "params": {"update": {"sessionUpdate": su, **extra}},
        }
    ) + "\n"


def test_bus_hot_and_collect_share_one_ear(tmp_path: Path):
    home = tmp_path / "Agent"
    (home / "asdaaas").mkdir(parents=True)
    sess = tmp_path / "sess"
    sess.mkdir()
    updates = sess / "updates.jsonl"
    events = sess / "events.jsonl"
    updates.write_text("")
    events.write_text("")

    bus = GrokNativeBus(
        sess,
        agent_home=home,
        agent_name="Agent",
        session_id=sess.name,
        start_offset=0,
    )
    # history before collect window → hot only
    with updates.open("a") as f:
        f.write(_line("agent_message_chunk", content={"type": "text", "text": "old\n"}))
    bus.catch_up()
    hot = home / "asdaaas" / "history" / "hot.jsonl"
    assert hot.exists()
    assert "old" in hot.read_text()

    fes = FileEventSource(sess, bus=bus)
    fes.open()  # collect window at tip
    u, e = fes.read_new_lines()
    assert u == []  # no new since window

    with updates.open("a") as f:
        f.write(_line("user_message_chunk", content={"type": "text", "text": "hi"}))
        f.write(
            _line(
                "tool_call",
                toolCallId="c1",
                title="t",
                status="running",
            )
        )
    with events.open("a") as f:
        f.write(json.dumps({"type": "turn_ended", "outcome": "completed"}) + "\n")

    u, e = fes.read_new_lines()
    sus = [
        (x.get("params") or {}).get("update", {}).get("sessionUpdate")
        for x in u
    ]
    assert "user_message_chunk" in sus
    assert "tool_call" in sus
    assert any(x.get("type") == "turn_ended" for x in e)
    # hot also got the new lines via same pump
    body = hot.read_text()
    assert "user_message_chunk" in body or "hi" in body or "tool" in body
    assert bus.updates.behind() == 0


def test_jsonl_byte_tail_partial_line(tmp_path: Path):
    from native_tail import JsonlByteTail

    p = tmp_path / "x.jsonl"
    p.write_bytes(b'{"a":1}\n{"b":')
    t = JsonlByteTail(p)
    b = t.read_batch()
    assert len(b.records) == 1
    assert t.offset == len(b'{"a":1}\n')
    # complete the line
    with p.open("ab") as f:
        f.write(b'2}\n')
    b2 = t.read_batch()
    assert len(b2.records) == 1
    assert "2" in b2.records[0].text


def test_occupancy_buffer_shares_bus(tmp_path: Path):
    home = tmp_path / "A"
    (home / "asdaaas").mkdir(parents=True)
    sess = tmp_path / "s"
    sess.mkdir()
    (sess / "updates.jsonl").write_text("")
    (sess / "events.jsonl").write_text("")
    bus = GrokNativeBus(
        sess, agent_home=home, agent_name="A", session_id=sess.name, start_offset=0
    )
    bus.begin_occupancy_window()
    with (sess / "updates.jsonl").open("a") as f:
        f.write(_line("agent_message_chunk", content={"type": "text", "text": "x"}))
        f.write(_line("turn_completed"))
    frames = bus.read_for_occupancy()
    assert len(frames) >= 2
    assert bus.updates.behind() == 0
    # second read empty until more writes
    assert bus.read_for_occupancy() == []
