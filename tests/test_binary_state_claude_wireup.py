"""Claude observer wire-up: session jsonl file → ClaudeInProcessObserver → state file.

The mapper tests cover line→event. These cover the plumbing around it: tailing the
real Claude session transcript, orienting from history, and reporting the model id
outward so health.json can stop saying "unknown".
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

from binary_state.service import ClaudeInProcessObserver  # noqa: E402
from binary_state.types import ObserverState  # noqa: E402


def _user(text="do a thing"):
    return {"type": "user", "message": {"content": text}, "timestamp": "2026-09-20T04:30:00.000Z"}


def _tool_use(tool_id="toolu_1", name="Bash"):
    return {
        "type": "assistant",
        "timestamp": "2026-09-20T04:30:01.000Z",
        "message": {
            "model": "claude-opus-5",
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": {}}],
        },
    }


def _tool_result(tool_id="toolu_1"):
    return {
        "type": "user",
        "timestamp": "2026-09-20T04:30:02.000Z",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id}]},
    }


def _end_turn():
    return {
        "type": "assistant",
        "timestamp": "2026-09-20T04:30:03.000Z",
        "message": {
            "model": "claude-opus-5",
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "done"}],
        },
    }


def _chrome():
    return {"type": "attachment", "timestamp": "2026-09-20T04:30:04.000Z", "content": {}}


def _write(path, lines):
    with open(path, "w") as f:
        for obj in lines:
            f.write(json.dumps(obj) + "\n")


def _append(path, lines):
    with open(path, "a") as f:
        for obj in lines:
            f.write(json.dumps(obj) + "\n")


@pytest.fixture
def session(tmp_path):
    return str(tmp_path / "de8ffa36.jsonl"), str(tmp_path / "observer_state.json")


def _observer(session_file, state_file, **kw):
    return ClaudeInProcessObserver(
        pid=os.getpid(),
        session_file=session_file,
        state_file=state_file,
        **kw,
    )


# --- orientation ----------------------------------------------------------


def test_orients_from_an_existing_transcript(session):
    """Startup mid-turn must reconstruct state from what already happened."""
    session_file, state_file = session
    _write(session_file, [_user(), _tool_use(), _chrome()])

    obs = _observer(session_file, state_file)
    obs.orient()

    assert obs.state == ObserverState.BUSY
    assert obs.state_dict()["pending_tools"] == {"toolu_1": "bash"}


def test_orientation_sees_a_finished_turn_as_idle(session):
    session_file, state_file = session
    _write(session_file, [_user(), _tool_use(), _tool_result(), _end_turn()])

    obs = _observer(session_file, state_file)
    obs.orient()

    assert obs.state == ObserverState.IDLE
    assert obs.state_dict()["pending_tools"] is None


def test_missing_session_file_is_survivable(session):
    """The transcript may not exist yet when the observer starts."""
    session_file, state_file = session
    obs = _observer(session_file, state_file)
    obs.orient()
    assert obs.state == ObserverState.STARTING


def test_state_file_is_written_on_orientation(session):
    session_file, state_file = session
    _write(session_file, [_user()])

    obs = _observer(session_file, state_file)
    obs.orient()

    assert os.path.exists(state_file)
    with open(state_file) as f:
        assert json.load(f)["state"] == ObserverState.BUSY.value


# --- tailing --------------------------------------------------------------


def test_new_lines_after_orientation_are_picked_up(session):
    session_file, state_file = session
    _write(session_file, [_user(), _tool_use()])

    obs = _observer(session_file, state_file)
    obs.orient()
    assert obs.state_dict()["pending_tools"] == {"toolu_1": "bash"}

    _append(session_file, [_tool_result(), _end_turn()])
    obs.poll_once()

    assert obs.state == ObserverState.IDLE
    assert obs.state_dict()["pending_tools"] is None


def test_orientation_does_not_replay_lines_on_the_first_poll(session):
    """seek_to_end after orienting — otherwise every line is counted twice."""
    session_file, state_file = session
    _write(session_file, [_user(), _tool_use()])

    obs = _observer(session_file, state_file)
    obs.orient()
    before = obs.state_dict()["turn_event_count"]
    obs.poll_once()

    assert obs.state_dict()["turn_event_count"] == before


def test_malformed_lines_are_skipped_not_fatal(session):
    session_file, state_file = session
    _write(session_file, [_user()])
    with open(session_file, "a") as f:
        f.write("{not json at all\n")
    _append(session_file, [_end_turn()])

    obs = _observer(session_file, state_file)
    obs.orient()
    assert obs.state == ObserverState.IDLE


# --- model identity -------------------------------------------------------


def test_model_id_is_reported_outward_once(session):
    """The callback asdaaas uses to put a real model in health.json."""
    session_file, state_file = session
    _write(session_file, [_user(), _tool_use(), _end_turn()])

    seen = []
    obs = _observer(
        session_file,
        state_file,
        on_model_id=lambda model_id, effort: seen.append(model_id),
    )
    obs.orient()

    assert seen == ["claude-opus-5"]  # once, not once per assistant line
    assert obs.state_dict()["model_id"] == "claude-opus-5"


def test_model_callback_failure_does_not_break_observation(session):
    session_file, state_file = session
    _write(session_file, [_user(), _tool_use()])

    def boom(model_id, effort):
        raise RuntimeError("health write failed")

    obs = _observer(session_file, state_file, on_model_id=boom)
    obs.orient()

    assert obs.state == ObserverState.BUSY
    assert obs.state_dict()["model_id"] == "claude-opus-5"


# --- lifecycle ------------------------------------------------------------


def test_stdout_events_are_ignored_not_fatal(session):
    """Claude has no stdout JSON-RPC plane; asdaaas may still call this."""
    session_file, state_file = session
    obs = _observer(session_file, state_file)
    obs.process_stdout_event({"method": "_x.ai/session_notification"})  # must not raise


def test_reset_repoints_at_the_new_session_file(session, tmp_path):
    session_file, state_file = session
    _write(session_file, [_user(), _tool_use()])
    obs = _observer(session_file, state_file)
    obs.orient()

    successor = str(tmp_path / "new-session.jsonl")
    _write(successor, [_user(), _end_turn()])
    obs.reset(os.getpid(), session_file=successor)

    assert obs.state == ObserverState.IDLE
    assert obs.state_dict()["pending_tools"] is None
