"""ActivityEvent adapters — grok mapper + machine; claude stub exists."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from binary_state.types import ActivityKind, ObserverState
from binary_state.grok import map_grok_updates_frame, GrokBinaryStateObserver
from binary_state.claude import map_claude_session_line, ClaudeBinaryStateObserver
from binary_state.machine import BinaryActivityMachine


KNOWN = {
    "user_message_chunk",
    "agent_message_chunk",
    "tool_call",
    "tool_call_update",
    "turn_completed",
}


def _frame(su, **extra):
    u = {"sessionUpdate": su, **extra}
    return {"timestamp": 1.0, "params": {"update": u}}


def test_map_grok_turn_start():
    ev = map_grok_updates_frame(_frame("user_message_chunk"), known_types=KNOWN)
    assert ev.kind == ActivityKind.TURN_START


def test_map_grok_tool_roundtrip_machine():
    m = BinaryActivityMachine(pid=1, process_alive_fn=lambda p: True)
    m.apply(map_grok_updates_frame(_frame("user_message_chunk"), known_types=KNOWN))
    assert m.state == ObserverState.BUSY
    m.apply(
        map_grok_updates_frame(
            _frame(
                "tool_call",
                toolCallId="t1",
                title="bash",
                _meta={"x.ai/tool": {"kind": "execute"}},
            ),
            known_types=KNOWN,
        )
    )
    assert m.has_pending_tool_calls
    m.apply(
        map_grok_updates_frame(
            _frame("tool_call_update", toolCallId="t1", status="completed"),
            known_types=KNOWN,
        )
    )
    assert not m.has_pending_tool_calls
    m.apply(map_grok_updates_frame(_frame("turn_completed"), known_types=KNOWN))
    assert m.state == ObserverState.IDLE


def test_claude_stub_exists():
    ev = map_claude_session_line({"type": "user", "message": {"content": "hi"}})
    assert ev is not None
    assert ev.kind == ActivityKind.UNKNOWN  # stub until Opus implements
    obs = ClaudeBinaryStateObserver(pid=1, process_alive_fn=lambda p: True)
    obs.process_event({"type": "user", "message": {"content": "hi"}})
    assert obs.state == ObserverState.UNKNOWN


def test_legacy_alias():
    from binary_state_observer import BinaryStateObserver, GrokBinaryStateObserver

    assert BinaryStateObserver is GrokBinaryStateObserver
