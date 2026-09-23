"""HotSpine reconcile + catch-up against native EOF."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

pytest.importorskip("zstandard")

from hot_spine import HotSpine, measure_behind  # noqa: E402
from stream_adapters.grok import tail_grok_once  # noqa: E402


def _line(su: str, **extra) -> str:
    return json.dumps(
        {
            "timestamp": time.time(),
            "method": "session/update",
            "params": {"update": {"sessionUpdate": su, **extra}},
        }
    ) + "\n"


def test_measure_behind_and_reconcile_catch_up(tmp_path: Path):
    home = tmp_path / "Agent"
    (home / "asdaaas").mkdir(parents=True)
    sess = tmp_path / "sess"
    sess.mkdir()
    updates = sess / "updates.jsonl"
    updates.write_text(_line("agent_message_chunk", content={"type": "text", "text": "hi"}))

    def sync():
        return tail_grok_once(home, "Agent", session_id=sess.name, source=updates)

    spine = HotSpine(
        agent_home=home,
        agent_name="Agent",
        sync_fn=sync,
        native_path=updates,
        reconcile_s=60.0,  # don't need loop noise
    )
    # no start() — unit test reconcile only
    st0 = measure_behind(agent_home=home, agent="Agent", native_path=updates)
    # no checkpoint yet → offset 0, behind = size
    assert st0.behind == updates.stat().st_size

    st = spine.reconcile(catch_up=True)
    assert st.caught_up or st.behind == 0
    assert st.behind == 0

    # Mid-tool stall simulation: more native bytes after checkpoint
    with updates.open("a") as f:
        f.write(
            _line(
                "tool_call",
                toolCallId="c1",
                title="t",
                status="running",
            )
        )
        f.write(
            _line(
                "tool_call_update",
                toolCallId="c1",
                status="completed",
            )
        )
        f.write(
            _line(
                "agent_message_chunk",
                content={"type": "text", "text": "Locked. r5\n"},
            )
        )

    st_b = measure_behind(agent_home=home, agent="Agent", native_path=updates)
    assert st_b.behind > 0
    st2 = spine.reconcile(catch_up=True)
    assert st2.behind == 0
    hot = home / "asdaaas" / "history" / "hot.jsonl"
    body = hot.read_text()
    assert "Locked" in body or "r5" in body


def test_start_stop_watcher_threads(tmp_path: Path):
    home = tmp_path / "A"
    (home / "asdaaas").mkdir(parents=True)
    sess = tmp_path / "s"
    sess.mkdir()
    updates = sess / "updates.jsonl"
    updates.write_text(_line("turn_completed"))

    n = {"c": 0}

    def sync():
        n["c"] += 1
        return tail_grok_once(home, "A", session_id=sess.name, source=updates)

    spine = HotSpine(
        agent_home=home,
        agent_name="A",
        sync_fn=sync,
        native_path=updates,
        reconcile_s=0.3,
    )
    spine.start()
    time.sleep(0.5)
    st = spine.status()
    assert st["watcher_alive"] is True
    assert st["reconcile_alive"] is True
    assert st["behind"] == 0
    spine.stop()
    time.sleep(0.2)
    # threads should stop
    assert n["c"] >= 1
