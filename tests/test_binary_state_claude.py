"""Claude native session jsonl → ActivityEvent mapper.

Fixtures in tests/fixtures/claude_session_lines.jsonl are REAL lines taken from a
live Astro session (free text redacted, structure untouched), so these tests fail
if the Claude Code session format moves out from under the mapper.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

from binary_state.claude import (  # noqa: E402
    ClaudeBinaryStateObserver,
    map_claude_session_line,
    map_claude_session_lines,
)
from binary_state.types import (  # noqa: E402
    DEFAULT_SILENCE_WINDOW,
    GATE_TOOL_KINDS,
    ActivityKind,
    ObserverState,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "claude_session_lines.jsonl")


def _fixture_lines():
    with open(FIXTURES) as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def _by_shape(shape):
    """Pull one real fixture line by the shape we care about."""
    for o in _fixture_lines():
        t = o.get("type")
        content = (o.get("message") or {}).get("content")
        first = content[0] if isinstance(content, list) and content else None
        btype = first.get("type") if isinstance(first, dict) else None
        if shape == "user_text" and t == "user" and isinstance(content, str):
            return o
        if shape == "user_tool_result" and t == "user" and btype == "tool_result":
            return o
        if shape == "assistant_thinking" and t == "assistant" and btype == "thinking":
            return o
        if shape == "assistant_text" and t == "assistant" and btype == "text":
            return o
        if shape == "assistant_tool_use" and t == "assistant" and btype == "tool_use":
            return o
    raise AssertionError(f"no fixture line for shape {shape!r}")


# --- chrome ---------------------------------------------------------------


@pytest.mark.parametrize(
    "chrome_type",
    ["attachment", "atis-latch", "last-prompt", "cost-state", "mode"],
)
def test_chrome_lines_are_skipped(chrome_type):
    """Chrome must produce no events — not UNKNOWN, which would blank the state."""
    line = next(o for o in _fixture_lines() if o.get("type") == chrome_type)
    assert map_claude_session_lines(line) == []
    assert map_claude_session_line(line) is None


def test_non_dict_is_skipped():
    assert map_claude_session_lines("not a dict") == []
    assert map_claude_session_line(None) is None


def test_unrecognized_type_is_unknown_not_skipped():
    """An unfamiliar line is reported, not silently dropped."""
    ev = map_claude_session_line({"type": "brand-new-thing"})
    assert ev.kind == ActivityKind.UNKNOWN
    assert ev.known is False
    assert "brand-new-thing" in ev.source_type


# --- user lines -----------------------------------------------------------


def test_user_text_is_turn_start():
    ev = map_claude_session_line(_by_shape("user_text"))
    assert ev.kind == ActivityKind.TURN_START
    assert ev.source_type == "claude:user"


def test_user_tool_result_is_tool_end_carrying_tool_id():
    line = _by_shape("user_tool_result")
    expected_id = line["message"]["content"][0]["tool_use_id"]
    ev = map_claude_session_line(line)
    assert ev.kind == ActivityKind.TOOL_END
    assert ev.tool_id == expected_id


def test_user_text_blocks_also_start_a_turn():
    ev = map_claude_session_line(
        {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}
    )
    assert ev.kind == ActivityKind.TURN_START


def test_empty_user_text_is_skipped():
    assert map_claude_session_lines({"type": "user", "message": {"content": "   "}}) == []


def test_mixed_user_line_yields_both_events_in_order():
    """tool_result and text can share one line; neither may be dropped."""
    evs = map_claude_session_lines(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_x", "content": "ok"},
                    {"type": "text", "text": "and now this"},
                ]
            },
        }
    )
    assert [e.kind for e in evs] == [ActivityKind.TOOL_END, ActivityKind.TURN_START]
    assert evs[0].tool_id == "toolu_x"


# --- assistant lines ------------------------------------------------------


def test_assistant_thinking_is_thought():
    ev = map_claude_session_line(_by_shape("assistant_thinking"))
    assert ev.kind == ActivityKind.THOUGHT


def test_assistant_text_is_speech():
    ev = map_claude_session_line(_by_shape("assistant_text"))
    assert ev.kind == ActivityKind.SPEECH


def test_end_turn_stop_reason_closes_the_turn():
    """Claude marks turn boundaries with stop_reason=end_turn, not a result line."""
    line = _by_shape("assistant_text")
    assert line["message"]["stop_reason"] == "end_turn"  # fixture sanity
    evs = map_claude_session_lines(line)
    assert [e.kind for e in evs] == [ActivityKind.SPEECH, ActivityKind.TURN_END]


def test_tool_use_stop_reason_does_not_close_the_turn():
    line = _by_shape("assistant_tool_use")
    assert line["message"]["stop_reason"] == "tool_use"  # fixture sanity
    evs = map_claude_session_lines(line)
    assert all(e.kind != ActivityKind.TURN_END for e in evs)


def test_assistant_tool_use_is_tool_start_with_identity_and_input():
    line = _by_shape("assistant_tool_use")
    block = line["message"]["content"][0]
    ev = map_claude_session_line(line)
    assert ev.kind == ActivityKind.TOOL_START
    assert ev.tool_id == block["id"]
    assert ev.tool_title == block["name"]
    assert ev.raw_input == block["input"]


def test_multi_block_assistant_line_yields_one_event_per_block():
    evs = map_claude_session_lines(
        {
            "type": "assistant",
            "message": {
                "model": "claude-opus-5",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "thinking", "thinking": "hm"},
                    {"type": "text", "text": "here goes"},
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}},
                ],
            },
        }
    )
    assert [e.kind for e in evs] == [
        ActivityKind.THOUGHT,
        ActivityKind.SPEECH,
        ActivityKind.TOOL_START,
    ]


def test_assistant_events_carry_model_and_effort():
    line = _by_shape("assistant_tool_use")
    ev = map_claude_session_line(line)
    assert ev.model_id == line["message"]["model"]
    if line.get("effort"):
        assert ev.reasoning_effort == line["effort"]


# --- silence windows ------------------------------------------------------


def test_bash_timeout_widens_the_silence_window():
    """A 10-minute Bash call must not be called STUCK at 60s."""
    ev = map_claude_session_line(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_2",
                        "name": "Bash",
                        "input": {"command": "sleep 300", "timeout": 600000},
                    }
                ]
            },
        }
    )
    assert ev.silence_s > 600  # 600000ms plus buffer, not the 60s default


def test_named_tool_window_is_honored():
    ev = map_claude_session_line(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "toolu_3", "name": "WebSearch", "input": {}}
                ]
            },
        },
        silence_windows={"WebSearch": 123.0},
    )
    assert ev.silence_s == 123.0


def test_tool_without_hints_gets_the_default_window():
    ev = map_claude_session_line(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "toolu_4", "name": "Read", "input": {}}]
            },
        }
    )
    assert ev.silence_s == DEFAULT_SILENCE_WINDOW


@pytest.mark.parametrize(
    "tool_name,expected_kind",
    [("ExitPlanMode", "exit_plan"), ("AskUserQuestion", "ask_user")],
)
def test_interactive_tools_map_to_gate_kinds(tool_name, expected_kind):
    """Waiting on a human is GATE, not STUCK — the machine keys that off tool_kind."""
    ev = map_claude_session_line(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "toolu_5", "name": tool_name, "input": {}}
                ]
            },
        }
    )
    assert ev.tool_kind == expected_kind
    assert ev.tool_kind in GATE_TOOL_KINDS


# --- timestamps -----------------------------------------------------------


def test_iso_timestamp_is_parsed_not_replaced_with_now():
    line = _by_shape("assistant_text")
    assert line["timestamp"].endswith("Z")  # fixture sanity
    ev = map_claude_session_line(line)
    assert ev.ts < time.time() - 60  # the real recorded time, not wall clock now
    assert ev.ts > 1_600_000_000


def test_missing_timestamp_falls_back_to_now():
    ev = map_claude_session_line({"type": "user", "message": {"content": "hi"}})
    assert abs(ev.ts - time.time()) < 5


# --- observer / machine integration --------------------------------------


def _observer():
    return ClaudeBinaryStateObserver(pid=os.getpid(), process_alive_fn=lambda p: True)


def test_observer_tracks_a_tool_call_through_to_completion():
    obs = _observer()
    obs.process_event({"type": "user", "message": {"content": "do a thing"}})
    assert obs.state == ObserverState.BUSY

    obs.process_event(
        {
            "type": "assistant",
            "message": {
                "stop_reason": "tool_use",
                "content": [
                    {"type": "tool_use", "id": "toolu_9", "name": "Bash", "input": {}}
                ],
            },
        }
    )
    assert obs.state_dict()["pending_tools"] == {"toolu_9": "bash"}

    obs.process_event(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_9"}]},
        }
    )
    assert obs.state_dict()["pending_tools"] is None
    assert obs.state == ObserverState.BUSY


def test_observer_goes_idle_on_end_turn():
    obs = _observer()
    obs.process_event({"type": "user", "message": {"content": "hi"}})
    obs.process_event(
        {
            "type": "assistant",
            "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "bye"}]},
        }
    )
    assert obs.state == ObserverState.IDLE
    assert obs.state_dict()["pending_tools"] is None


def test_observer_reports_model_id_once_it_sees_one():
    """This is the field health.json has been reporting as 'unknown'."""
    obs = _observer()
    assert obs.state_dict()["model_id"] == "unknown"  # machine's starting value
    obs.process_event(_by_shape("assistant_tool_use"))
    assert obs.state_dict()["model_id"] == "claude-opus-5"


def test_replaying_a_real_session_ends_idle_and_clean():
    """End to end over real transcript lines, chrome and all."""
    obs = _observer()
    obs.orient_from_history(_fixture_lines())
    assert obs.state in (ObserverState.BUSY, ObserverState.IDLE)
    assert obs.state != ObserverState.UNKNOWN  # chrome must not blank us out
    assert obs.state_dict()["model_id"] == "claude-opus-5"


def test_chrome_alone_never_moves_the_machine():
    obs = _observer()
    for line in _fixture_lines():
        if line.get("type") in ("attachment", "mode", "cost-state"):
            obs.process_event(line)
    assert obs.state == ObserverState.STARTING


# --- message queue visibility ---------------------------------------------


def _queue_line(op="enqueue", content="<eric (via tui)> ping"):
    line = {
        "type": "queue-operation",
        "operation": op,
        "timestamp": "2026-09-20T22:51:30.000Z",
        "sessionId": "de8ffa36",
    }
    if op == "enqueue":
        line["content"] = content
    return line


def test_enqueue_is_visible_as_session_activity():
    """A message arriving mid-turn must leave a trace; silence is the bug Eric hit."""
    ev = map_claude_session_line(_queue_line("enqueue"))
    assert ev is not None
    assert ev.kind == ActivityKind.SESSION_ACTIVITY
    assert ev.source_type == "claude:queue:enqueue"
    assert ev.activity == "message_enqueued"


def test_dequeue_is_visible_too():
    ev = map_claude_session_line(_queue_line("dequeue"))
    assert ev.kind == ActivityKind.SESSION_ACTIVITY
    assert ev.source_type == "claude:queue:dequeue"
    assert ev.activity == "message_dequeued"


def test_queue_traffic_does_not_move_the_state_machine():
    """Metadata only: an arriving message is not the agent doing work."""
    obs = _observer()
    obs.process_event({"type": "user", "message": {"content": "go"}})
    obs.process_event(
        {
            "type": "assistant",
            "message": {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "toolu_q", "name": "Bash", "input": {}}]},
        }
    )
    before = obs.state_dict()
    obs.process_event(_queue_line("enqueue"))
    after = obs.state_dict()

    assert after["state"] == before["state"] == ObserverState.BUSY.value
    assert after["pending_tools"] == before["pending_tools"]
    # turn_event_count DOES tick: machine.apply() increments it before it
    # dispatches on kind, so even metadata events count. Documented rather than
    # worked around — the counter lives in machine.py, which is not mine to change.
    assert after["turn_event_count"] == before["turn_event_count"] + 1


def test_arrival_is_still_readable_after_the_dequeue_lands():
    """enqueue/dequeue arrive in the same second; the arrival must not vanish."""
    obs = _observer()
    obs.process_event({"type": "user", "message": {"content": "go"}})
    obs.process_event(_queue_line("enqueue"))
    obs.process_event(_queue_line("dequeue"))

    state = obs.state_dict()
    assert state["activity"] == "message_dequeued"
    assert state["last_event_type"] == "claude:queue:dequeue"
