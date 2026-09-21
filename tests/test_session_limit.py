"""session_limit detect + park."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from session_limit import (
    inspect_limit_text,
    parse_reset_unix,
    write_park_state,
    read_park_state,
    clear_park_state,
    SessionLimitInfo,
)


def test_detect_session_limit_phrase():
    info = inspect_limit_text("You've hit your session limit. Resets at 2:20am PT.")
    assert info.detected
    assert info.reset_unix is not None
    assert info.reset_unix > time.time()


def test_try_again_in_hours():
    info = inspect_limit_text("rate limit exceeded, try again in 4 hours")
    assert info.detected
    assert abs((info.reset_unix or 0) - (time.time() + 4 * 3600)) < 5


def test_park_roundtrip(tmp_path):
    info = SessionLimitInfo(
        detected=True, source="test", raw_snippet="limit", reset_unix=time.time() + 100
    )
    write_park_state(tmp_path, info)
    data = read_park_state(tmp_path)
    assert data["status"] == "session_limited"
    clear_park_state(tmp_path)
    assert read_park_state(tmp_path) is None


def test_no_false_positive():
    assert not inspect_limit_text("session loaded successfully").detected
