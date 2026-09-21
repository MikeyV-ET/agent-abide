"""Expired parks must wake+clear; long parks schedule restart."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from session_limit import (  # noqa: E402
    SessionLimitInfo,
    write_park_state,
    read_park_state,
    should_hold_park,
    reconcile_session_park,
    handle_session_limit,
)


def test_reconcile_clears_expired_park(tmp_path: Path, monkeypatch):
    now = time.time()
    info = SessionLimitInfo(
        detected=True,
        reason="session_limit",
        reset_unix=now - 3600,  # already past
        raw_snippet="resets 11:40am",
    )
    write_park_state(tmp_path, info)
    assert read_park_state(tmp_path) is not None
    assert should_hold_park(tmp_path, now=now) is False

    monkeypatch.setattr(
        "session_limit.agent_dir" if False else "asdaaas.agent_dir",
        lambda name, env=None: tmp_path,
        raising=False,
    )
    # Patch imports inside functions
    import asdaaas as aa

    monkeypatch.setattr(aa, "agent_dir", lambda name, env=None: tmp_path)
    monkeypatch.setattr(aa, "write_conversation", lambda *a, **k: None)
    monkeypatch.setattr(aa, "write_health", lambda *a, **k: None)
    monkeypatch.setattr(aa, "queue_continue_doorbell", lambda *a, **k: None)

    rec = reconcile_session_park("Astro", env=None, now=now)
    assert rec.get("woke") is True
    assert read_park_state(tmp_path) is None


def test_reconcile_holds_before_reset(tmp_path: Path, monkeypatch):
    now = time.time()
    info = SessionLimitInfo(
        detected=True,
        reason="session_limit",
        reset_unix=now + 7200,
    )
    write_park_state(tmp_path, info)
    import asdaaas as aa

    monkeypatch.setattr(aa, "agent_dir", lambda name, env=None: tmp_path)
    rec = reconcile_session_park("Astro", env=None, now=now)
    assert rec.get("holding") is True
    assert rec.get("remaining_s", 0) > 7000
    assert read_park_state(tmp_path) is not None


def test_handle_schedules_restart_even_for_long_park(tmp_path: Path, monkeypatch):
    now = time.time()
    info = SessionLimitInfo(
        detected=True,
        reason="session_limit",
        reset_unix=now + 10000,  # >2h
        raw_snippet="resets 11:40am (America/Los_Angeles)",
    )
    import asdaaas as aa

    monkeypatch.setattr(aa, "agent_dir", lambda name, env=None: tmp_path)
    monkeypatch.setattr(aa, "write_health", lambda *a, **k: None)
    monkeypatch.setattr(aa, "write_conversation", lambda *a, **k: None)
    called = {}

    def fake_restart(name, *, reason="", delay_s=2.0):
        called["delay_s"] = delay_s
        called["reason"] = reason
        return True

    monkeypatch.setattr(aa, "schedule_self_restart", fake_restart)
    out = handle_session_limit("Astro", info, env=None)
    assert called.get("delay_s", 0) >= 9000
    assert out["delay_s"] <= 600  # chunked in-process
    assert out.get("delay_s_full", 0) >= 9000


def test_write_park_preserves_original_t1(tmp_path: Path):
    now = time.time()
    info1 = SessionLimitInfo(
        detected=True, reason="session_limit", reset_unix=now + 5000,
        raw_snippet="first",
    )
    write_park_state(tmp_path, info1)
    p1 = read_park_state(tmp_path)
    t1 = p1["parked_at"]
    time.sleep(0.05)
    info2 = SessionLimitInfo(
        detected=True, reason="session_limit", reset_unix=now + 4000,
        raw_snippet="re-park",
    )
    write_park_state(tmp_path, info2)
    p2 = read_park_state(tmp_path)
    assert p2["parked_at"] == t1
    assert p2["parked_at_iso"] == p1["parked_at_iso"]
    assert p2["snippet"] == "re-park"
    assert "updated_at" in p2


def test_handle_expired_reset_does_not_repark(tmp_path: Path, monkeypatch):
    now = time.time()
    info = SessionLimitInfo(
        detected=True,
        reason="session_limit",
        reset_unix=now - 100,
        raw_snippet="resets 11:40am",
    )
    import asdaaas as aa
    monkeypatch.setattr(aa, "agent_dir", lambda name, env=None: tmp_path)
    monkeypatch.setattr(aa, "write_health", lambda *a, **k: None)
    monkeypatch.setattr(aa, "write_conversation", lambda *a, **k: None)
    monkeypatch.setattr(aa, "schedule_self_restart", lambda *a, **k: True)
    # seed old park with real T1
    old = SessionLimitInfo(
        detected=True, reason="session_limit", reset_unix=now - 100
    )
    write_park_state(tmp_path, old)
    real_t1 = read_park_state(tmp_path)["parked_at"]
    out = handle_session_limit("Astro", info, env=None)
    assert out.get("already_expired") is True
    assert out.get("parked") is False
    # should have cleared via reconcile
    assert read_park_state(tmp_path) is None
