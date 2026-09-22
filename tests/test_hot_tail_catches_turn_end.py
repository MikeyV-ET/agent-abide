"""hot must ingest tool_result + final speech + turn_completed after mid-tool stall."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

pytest.importorskip("zstandard")

from grok_backend import GrokBackend  # noqa: E402
from stream_adapters.grok import tail_grok_once  # noqa: E402


def _su(session_update: str, **extra) -> str:
    upd = {"sessionUpdate": session_update, **extra}
    return json.dumps(
        {
            "timestamp": 1700000000.0,
            "method": "session/update",
            "params": {"update": upd},
        }
    ) + "\n"


def test_tail_catches_completion_after_open_tool(tmp_path: Path):
    """Reproduce Squiggy glass stuck on open tool while updates has the end.

    Sequence: tool_call start lands and is tailed; later tool completed +
    agent speech + turn_completed appear. A subsequent tail must ingest them
    without requiring a new user turn.
    """
    home = tmp_path / "Squiggy"
    (home / "asdaaas").mkdir(parents=True)
    sess = tmp_path / "session"
    sess.mkdir()
    updates = sess / "updates.jsonl"

    tool_id = "call-stall-test-1"
    # Phase 1: open tool only
    updates.write_text(
        _su(
            "tool_call",
            toolCallId=tool_id,
            title="run_terminal_command",
            status="running",
        )
        + _su(
            "tool_call_update",
            toolCallId=tool_id,
            title="Execute `sleep`",
            status="running",
        )
    )

    b = GrokBackend()
    b._session_id = sess.name
    b._session_dir = sess
    # Enable ingest without starting watcher (deterministic; no race with initial sync)
    b._agent_home = home
    b._agent_name = "Squiggy"
    b._hot_ingest = True

    r1 = b.sync_hot_stream()
    assert r1.get("status") == "ok", r1
    assert r1.get("lines_ingested") >= 1

    hot = home / "asdaaas" / "history" / "hot.jsonl"
    phases = [json.loads(l).get("phase") for l in hot.read_text().splitlines() if l.strip()]
    assert "start" in phases or "delta" in phases

    # Phase 2: completion tail that used to sit unread on live Squiggy
    with updates.open("a") as f:
        f.write(
            _su(
                "tool_call_update",
                toolCallId=tool_id,
                status="completed",
                content=[{"type": "content", "content": {"type": "text", "text": "ok\n"}}],
            )
        )
        f.write(
            _su(
                "agent_message_chunk",
                content={"type": "text", "text": "Locked. r5 is the keep.\n"},
            )
        )
        f.write(_su("turn_completed", prompt_id="p1", stop_reason="end_turn"))

    # No new user turn — only final hot sync (what collect/turn_engine must do)
    b._final_hot_sync()
    r2 = b.sync_hot_stream()  # idempotent
    assert r2.get("lines_ingested") == 0  # already caught by _final_hot_sync

    evs = [json.loads(l) for l in hot.read_text().splitlines() if l.strip()]
    classes_phases = [(e.get("class"), e.get("phase"), (e.get("body") or {}).get("kind")) for e in evs]
    # Must have tool end + assistant speech + turn_completed meta
    assert any(c == "tool" and p == "end" for c, p, _ in classes_phases), classes_phases
    texts = [
        (e.get("body") or {}).get("text", "")
        for e in evs
        if e.get("class") == "message" and e.get("role") == "assistant"
    ]
    assert any("Locked" in (t or "") for t in texts), texts
    assert any(
        (e.get("body") or {}).get("label") == "turn_completed"
        for e in evs
        if e.get("class") == "meta"
    ), classes_phases


def test_tail_grok_once_direct_gap(tmp_path: Path):
    """Adapter alone advances past a mid-tool checkpoint."""
    home = tmp_path / "A"
    (home / "asdaaas").mkdir(parents=True)
    sess = tmp_path / "s"
    sess.mkdir()
    updates = sess / "updates.jsonl"
    tid = "call-x"
    updates.write_text(_su("tool_call", toolCallId=tid, title="t", status="running"))
    r1 = tail_grok_once(home, "A", session_id=sess.name, source=updates)
    assert r1["lines_ingested"] == 1
    with updates.open("a") as f:
        f.write(_su("tool_call_update", toolCallId=tid, status="completed"))
        f.write(_su("agent_message_chunk", content={"type": "text", "text": "done"}))
        f.write(_su("turn_completed"))
    r2 = tail_grok_once(home, "A", session_id=sess.name, source=updates)
    assert r2["lines_ingested"] == 3
    assert r2["offset_after"] == updates.stat().st_size
