"""retry_state from hot native must paint (parity with prod updates.jsonl)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import (  # noqa: E402
    line_to_tui_event,
    is_tip_paint_event,
    collapse_tip_events,
)


def _hot_retry_line(attempt: int = 1) -> str:
    return json.dumps(
        {
            "format": "aa.stream",
            "v": 1,
            "stream_seq": 100,
            "class": "unknown",
            "backend": "grok",
            "body": {"kind": "raw_only"},
            "native": {
                "schema": "grok.session_update.v1",
                "event": {
                    "timestamp": 1,
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "retry_state",
                            "type": "retrying",
                            "attempt": attempt,
                            "max_retries": 15,
                            "reason": (
                                "API error (status 500 Internal Server Error): "
                                "error: Service temporarily unavailable. "
                                "The model did not respond to this request."
                            ),
                            "error_type": "api",
                        }
                    },
                },
            },
        }
    )


def test_retry_raw_only_native_becomes_paint_event():
    ev = line_to_tui_event(_hot_retry_line(3), "hot")
    assert ev is not None
    u = (ev.get("params") or {}).get("update") or {}
    assert u.get("sessionUpdate") == "retry_state"
    assert u.get("attempt") == 3
    assert "unavailable" in (u.get("reason") or "")
    assert is_tip_paint_event(ev)


def test_collapse_keeps_retry():
    events = [
        line_to_tui_event(_hot_retry_line(1), "hot"),
        line_to_tui_event(_hot_retry_line(2), "hot"),
    ]
    events = [e for e in events if e]
    out = collapse_tip_events(events)
    assert len(out) == 2
    assert all(
        ((e.get("params") or {}).get("update") or {}).get("sessionUpdate")
        == "retry_state"
        for e in out
    )
