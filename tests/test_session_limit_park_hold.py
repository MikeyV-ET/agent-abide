"""A/C: park hold ignores input; wake notice carries reset + T1→T2 + memory nudge."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from session_limit import (  # noqa: E402
    SessionLimitInfo,
    write_park_state,
    clear_park_state,
    should_hold_park,
    park_remaining_s,
    format_wake_notice,
    count_queued_inputs,
)


def test_should_hold_park_before_reset(tmp_path: Path):
    now = time.time()
    info = SessionLimitInfo(
        detected=True,
        reason="session_limit",
        source="test",
        raw_snippet="resets 8:40pm",
        reset_unix=now + 3600,
    )
    write_park_state(tmp_path, info)
    assert should_hold_park(tmp_path, now=now) is True
    assert should_hold_park(tmp_path, now=now + 7200) is False
    rem = park_remaining_s(tmp_path, now=now)
    assert rem is not None and 3500 < rem <= 3600
    clear_park_state(tmp_path)
    assert should_hold_park(tmp_path, now=now) is False


def test_should_hold_park_unknown_reset(tmp_path: Path):
    info = SessionLimitInfo(detected=True, reason="session_limit", reset_unix=None)
    write_park_state(tmp_path, info)
    assert should_hold_park(tmp_path) is True


def test_format_wake_notice_has_reset_t1_t2_memory(tmp_path: Path):
    now = time.time()
    info = SessionLimitInfo(
        detected=True,
        reason="session_limit",
        raw_snippet="You've hit your session limit · resets 8:40pm (America/Los_Angeles)",
        reset_unix=now + 100,
    )
    write_park_state(tmp_path, info)
    park = json.loads((tmp_path / "session_limit.json").read_text())
    # fake queue
    (tmp_path / "doorbells").mkdir()
    (tmp_path / "doorbells" / "bell_x.json").write_text("{}")
    (tmp_path / "doorbells" / "cont_y.json").write_text("{}")
    q = count_queued_inputs(tmp_path)
    assert q["doorbells"] == 1
    assert q["continues"] == 1
    assert q["total"] == 1
    text = format_wake_notice(park, queued=q, now=now + 50)
    assert "T1=" in text and "T2=" in text
    assert "Parsed reset" in text
    assert "Queued while parked: 1" in text
    assert "memory_query" in text.lower() or "memory_recall" in text.lower()
    assert "session_limit cleared" in text


def test_format_wake_distinguishes_real_wake_fields():
    park = {
        "status": "session_limited",
        "parked_at_iso": "2026-09-21T01:00:00+00:00",
        "reset_unix": 1790000000.0,
        "reset_iso": "2026-09-21T08:40:00+00:00",
    }
    text = format_wake_notice(park, queued={"total": 3, "doorbells": 2, "adapter_files": 1})
    assert "2026-09-21T01:00:00" in text
    assert "3 item" in text
