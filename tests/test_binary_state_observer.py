"""
test_binary_state_observer.py -- Contract tests for the Binary State Observer.

Tests the observer's event-processing contract: given an event sequence from
updates.jsonl, what state should the observer report?

Written test-first from the spec at:
    ~/agents/Trip/AA-architecture-audit/binary_state_observer_spec.md

The observer is a separate process that tails updates.jsonl + checks process
liveness, then writes an atomic state file. These tests exercise the core
state machine logic in isolation -- no file tailing, no process management.

Run: cd ~/projects/agent-abide && python3 -m pytest tests/test_binary_state_observer.py -v
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from binary_state_observer import BinaryStateObserver, ObserverState


# ============================================================================
# Helpers — build updates.jsonl frames
# ============================================================================

def make_frame(session_update: str, extra: dict = None):
    """Build a single updates.jsonl frame."""
    frame = {"params": {"update": {"sessionUpdate": session_update}}}
    if extra:
        frame["params"]["update"].update(extra)
    return frame


def user_message():
    return make_frame("user_message_chunk", {"content": {"text": "hello"}})


def agent_message(text="response"):
    return make_frame("agent_message_chunk", {"content": {"text": text}})


def agent_thought(text="thinking"):
    return make_frame("agent_thought_chunk", {"content": {"text": text}})


def turn_completed():
    return make_frame("turn_completed")


def tool_call(tool_id="t1", tool_name="run_terminal_command", raw_input=None):
    extra = {"toolCallId": tool_id, "title": tool_name}
    if raw_input:
        extra["rawInput"] = raw_input
    return make_frame("tool_call", extra)


def tool_call_update(tool_id="t1", status="completed"):
    return make_frame("tool_call_update", {"toolCallId": tool_id, "status": status})


def retry_state(retry_type="retrying", attempt=1, reason="no_visible_content"):
    return make_frame("retry_state", {
        "type": retry_type,
        "attempt": attempt,
        "reason": reason,
    })


def doom_loop():
    return make_frame("doom_loop_detected")


def unknown_event(event_type="completely_new_event_type"):
    return make_frame(event_type)


# ============================================================================
# Fixtures
# ============================================================================

KNOWN_TYPES = [
    "user_message_chunk", "agent_message_chunk", "agent_thought_chunk",
    "tool_call", "tool_call_update", "turn_completed", "turn_started",
    "retry_state", "doom_loop_detected", "system_message",
    "auto_compact_started", "auto_compact_completed",
    # Include all 51 types from spec -- these are the ones the observer
    # should recognize. The full list comes from the types data file.
]


@pytest.fixture
def observer():
    """Create an observer with process assumed alive, no history."""
    obs = BinaryStateObserver(
        pid=12345,
        known_types=set(KNOWN_TYPES),
        process_alive_fn=lambda pid: True,  # mock: always alive
    )
    return obs


@pytest.fixture
def dead_process_observer():
    """Observer where process is reported dead."""
    obs = BinaryStateObserver(
        pid=12345,
        known_types=set(KNOWN_TYPES),
        process_alive_fn=lambda pid: False,
    )
    return obs


# ============================================================================
# 1. Initial state
# ============================================================================

class TestInitialState:
    """Observer starts in STARTING before any events are processed."""

    def test_fresh_start_is_starting(self, observer):
        assert observer.state == ObserverState.STARTING

    def test_starting_has_no_last_event(self, observer):
        assert observer.last_event_type is None

    def test_starting_has_pid(self, observer):
        assert observer.pid == 12345


# ============================================================================
# 2. Basic state transitions
# ============================================================================

class TestBasicTransitions:
    """Core state machine: STARTING → BUSY → IDLE."""

    def test_user_message_transitions_to_busy(self, observer):
        observer.process_event(user_message())
        assert observer.state == ObserverState.BUSY

    def test_turn_completed_transitions_to_idle(self, observer):
        observer.process_event(user_message())
        observer.process_event(turn_completed())
        assert observer.state == ObserverState.IDLE

    def test_full_turn_cycle(self, observer):
        """STARTING → BUSY → (agent messages) → IDLE."""
        assert observer.state == ObserverState.STARTING

        observer.process_event(user_message())
        assert observer.state == ObserverState.BUSY

        observer.process_event(agent_thought())
        assert observer.state == ObserverState.BUSY

        observer.process_event(agent_message())
        assert observer.state == ObserverState.BUSY

        observer.process_event(turn_completed())
        assert observer.state == ObserverState.IDLE

    def test_second_turn(self, observer):
        """After IDLE, another user_message returns to BUSY."""
        observer.process_event(user_message())
        observer.process_event(turn_completed())
        assert observer.state == ObserverState.IDLE

        observer.process_event(user_message())
        assert observer.state == ObserverState.BUSY

    def test_multiple_turns(self, observer):
        """Three full turns cycle correctly."""
        for _ in range(3):
            observer.process_event(user_message())
            assert observer.state == ObserverState.BUSY
            observer.process_event(agent_message())
            observer.process_event(turn_completed())
            assert observer.state == ObserverState.IDLE


# ============================================================================
# 3. Retry detection
# ============================================================================

class TestRetryDetection:
    """retry_state events transition to RETRYING or back to BUSY."""

    def test_retry_state_transitions_to_retrying(self, observer):
        observer.process_event(user_message())
        observer.process_event(retry_state("retrying", attempt=1))
        assert observer.state == ObserverState.RETRYING

    def test_retry_tracks_attempt(self, observer):
        observer.process_event(user_message())
        observer.process_event(retry_state("retrying", attempt=3))
        assert observer.retry_attempt == 3

    def test_retry_tracks_reason(self, observer):
        observer.process_event(user_message())
        observer.process_event(retry_state("retrying", reason="http_error"))
        assert observer.retry_reason == "http_error"

    def test_retry_failed_returns_to_busy(self, observer):
        """retry_state type=failed means retry resolved, back in turn."""
        observer.process_event(user_message())
        observer.process_event(retry_state("retrying", attempt=1))
        assert observer.state == ObserverState.RETRYING

        observer.process_event(retry_state("failed"))
        assert observer.state == ObserverState.BUSY

    def test_retry_then_normal_completion(self, observer):
        """Retry followed by successful turn completion."""
        observer.process_event(user_message())
        observer.process_event(retry_state("retrying", attempt=1))
        observer.process_event(agent_message())
        observer.process_event(turn_completed())
        assert observer.state == ObserverState.IDLE


# ============================================================================
# 4. Tool call tracking
# ============================================================================

class TestToolCallTracking:
    """Tool calls add to pending set; completions remove them."""

    def test_tool_call_adds_pending(self, observer):
        observer.process_event(user_message())
        observer.process_event(tool_call("t1"))
        assert observer.has_pending_tool_calls
        assert observer.turn_event_count >= 2

    def test_tool_complete_removes_pending(self, observer):
        observer.process_event(user_message())
        observer.process_event(tool_call("t1"))
        observer.process_event(tool_call_update("t1", "completed"))
        assert not observer.has_pending_tool_calls

    def test_tool_failed_removes_pending(self, observer):
        observer.process_event(user_message())
        observer.process_event(tool_call("t1"))
        observer.process_event(tool_call_update("t1", "failed"))
        assert not observer.has_pending_tool_calls

    def test_multiple_concurrent_tools(self, observer):
        observer.process_event(user_message())
        observer.process_event(tool_call("t1"))
        observer.process_event(tool_call("t2"))
        observer.process_event(tool_call("t3"))
        assert observer.has_pending_tool_calls

        observer.process_event(tool_call_update("t1", "completed"))
        assert observer.has_pending_tool_calls  # t2, t3 still pending

        observer.process_event(tool_call_update("t2", "completed"))
        observer.process_event(tool_call_update("t3", "completed"))
        assert not observer.has_pending_tool_calls

    def test_new_turn_clears_pending_tools(self, observer):
        """user_message_chunk clears pending tools (new turn)."""
        observer.process_event(user_message())
        observer.process_event(tool_call("t1"))
        assert observer.has_pending_tool_calls

        observer.process_event(user_message())  # new turn
        assert not observer.has_pending_tool_calls


# ============================================================================
# 5. doom_loop_detected
# ============================================================================

class TestDoomLoop:
    """doom_loop_detected sets a flag, cleared on new turn."""

    def test_doom_loop_sets_flag(self, observer):
        observer.process_event(user_message())
        observer.process_event(doom_loop())
        assert observer.doom_loop is True

    def test_doom_loop_clears_on_new_turn(self, observer):
        observer.process_event(user_message())
        observer.process_event(doom_loop())
        assert observer.doom_loop is True

        observer.process_event(turn_completed())
        observer.process_event(user_message())  # new turn
        assert observer.doom_loop is False


# ============================================================================
# 6. UNKNOWN events
# ============================================================================

class TestUnknownEvents:
    """Unrecognized event types → UNKNOWN, but observer continues."""

    def test_unknown_event_type(self, observer):
        observer.process_event(user_message())
        observer.process_event(unknown_event("brand_new_binary_feature"))
        assert observer.state == ObserverState.UNKNOWN

    def test_unknown_records_event_type(self, observer):
        observer.process_event(user_message())
        observer.process_event(unknown_event("brand_new_binary_feature"))
        assert observer.unknown_event == "brand_new_binary_feature"

    def test_unknown_continues_tracking_known_events(self, observer):
        """After UNKNOWN, known events still update state normally."""
        observer.process_event(user_message())
        observer.process_event(unknown_event("mystery"))
        assert observer.state == ObserverState.UNKNOWN

        # Known event restores normal state tracking
        observer.process_event(agent_message())
        assert observer.state == ObserverState.BUSY

        observer.process_event(turn_completed())
        assert observer.state == ObserverState.IDLE


# ============================================================================
# 7. GONE — process liveness
# ============================================================================

class TestProcessGone:
    """Process death → GONE with exit code."""

    def test_gone_on_heartbeat(self, dead_process_observer):
        obs = dead_process_observer
        obs.process_event(user_message())
        obs.check_heartbeat()
        assert obs.state == ObserverState.GONE

    def test_gone_overrides_busy(self, observer):
        """Even if last event said BUSY, dead process = GONE."""
        observer.process_event(user_message())
        assert observer.state == ObserverState.BUSY

        # Switch to dead process
        observer._process_alive_fn = lambda pid: False
        observer.check_heartbeat()
        assert observer.state == ObserverState.GONE


# ============================================================================
# 8. STUCK — contextual silence windows
# ============================================================================

class TestStuckDetection:
    """STUCK fires when silence exceeds expected window for context."""

    def test_stuck_after_silence(self, observer):
        """Silence after agent message (expected: sub-second) → STUCK."""
        observer.process_event(user_message())
        observer.process_event(agent_message())

        # Simulate time passing beyond expected silence window
        observer._last_event_ts = time.time() - 120  # 2 min ago
        observer.check_heartbeat()
        assert observer.state == ObserverState.STUCK

    def test_not_stuck_within_window(self, observer):
        """Recent event within expected window → still BUSY."""
        observer.process_event(user_message())
        observer.process_event(agent_message())
        # last_event_ts is fresh (just now)
        observer.check_heartbeat()
        assert observer.state == ObserverState.BUSY

    def test_not_stuck_when_idle(self, observer):
        """Silence when IDLE is normal — no STUCK."""
        observer.process_event(user_message())
        observer.process_event(turn_completed())
        observer._last_event_ts = time.time() - 3600  # 1 hour ago
        observer.check_heartbeat()
        assert observer.state == ObserverState.IDLE  # not STUCK

    def test_tool_call_with_explicit_timeout_extends_window(self, observer):
        """Tool call with timeout in rawInput → expected silence = timeout."""
        observer.process_event(user_message())
        observer.process_event(tool_call(
            "t1", "wait_commands_or_subagents",
            raw_input=json.dumps({"timeout_ms": 600000})  # 10 min
        ))
        # 5 minutes of silence — within the 10-min timeout
        observer._last_event_ts = time.time() - 300
        observer.check_heartbeat()
        assert observer.state == ObserverState.BUSY  # NOT stuck

    def test_tool_call_with_explicit_timeout_still_stuck_after(self, observer):
        """Silence exceeding explicit timeout → STUCK."""
        observer.process_event(user_message())
        observer.process_event(tool_call(
            "t1", "wait_commands_or_subagents",
            raw_input=json.dumps({"timeout_ms": 60000})  # 1 min
        ))
        # 5 minutes of silence — exceeds the 1-min timeout (with buffer)
        observer._last_event_ts = time.time() - 300
        observer.check_heartbeat()
        assert observer.state == ObserverState.STUCK


# ============================================================================
# 9. Jr T87 pattern — long subagent wait should not STUCK
# ============================================================================

class TestLongToolCallNotStuck:
    """
    Jr's T87: spawned 3 worktree subagents, waited 37 minutes.
    Binary had continuous tool_call activity throughout. Observer should
    never report STUCK because events kept flowing.
    """

    def test_long_tool_call_with_periodic_activity(self, observer):
        """Periodic tool_call_update events reset the silence clock."""
        observer.process_event(user_message())

        # Spawn 3 subagents (tool calls)
        observer.process_event(tool_call("sub1", "spawn_subagent"))
        observer.process_event(tool_call("sub2", "spawn_subagent"))
        observer.process_event(tool_call("sub3", "spawn_subagent"))

        # Wait call with 10-minute timeout
        observer.process_event(tool_call(
            "wait1", "wait_commands_or_subagents",
            raw_input=json.dumps({"timeout_ms": 600000})
        ))

        # Simulate 37 minutes of periodic activity (every ~2 min)
        for i in range(18):
            # Each update resets the silence clock
            observer.process_event(tool_call_update(f"sub{(i % 3) + 1}", "running"))
            observer.check_heartbeat()
            assert observer.state == ObserverState.BUSY, \
                f"Should be BUSY at activity update {i}, not {observer.state}"

        # Final completions
        observer.process_event(tool_call_update("sub1", "completed"))
        observer.process_event(tool_call_update("sub2", "completed"))
        observer.process_event(tool_call_update("sub3", "completed"))
        observer.process_event(tool_call_update("wait1", "completed"))

        observer.process_event(agent_message("done"))
        observer.process_event(turn_completed())
        assert observer.state == ObserverState.IDLE


# ============================================================================
# 10. Metadata fields
# ============================================================================

class TestMetadata:
    """State file includes metadata: since, last_event, turn_event_count."""

    def test_last_event_type_tracked(self, observer):
        observer.process_event(user_message())
        assert observer.last_event_type == "user_message_chunk"

        observer.process_event(agent_message())
        assert observer.last_event_type == "agent_message_chunk"

    def test_since_updates_on_state_change(self, observer):
        observer.process_event(user_message())
        busy_since = observer.since
        assert busy_since is not None

        observer.process_event(turn_completed())
        idle_since = observer.since
        assert idle_since >= busy_since

    def test_turn_event_count(self, observer):
        observer.process_event(user_message())
        assert observer.turn_event_count == 1

        observer.process_event(agent_thought())
        observer.process_event(agent_message())
        assert observer.turn_event_count == 3

        observer.process_event(turn_completed())
        assert observer.turn_event_count == 0  # reset after turn

    def test_state_dict_structure(self, observer):
        """state_dict() returns the full state file contents."""
        observer.process_event(user_message())
        observer.process_event(tool_call("t1"))

        d = observer.state_dict()
        assert d["state"] == "BUSY"
        assert d["pid"] == 12345
        assert "since" in d
        assert "last_event_type" in d
        assert "last_event_ts" in d
        assert d["turn_event_count"] >= 2
        assert d["doom_loop"] is False


# ============================================================================
# 11. Orientation from existing history
# ============================================================================

class TestOrientation:
    """Observer orients from existing updates.jsonl on startup."""

    def test_orient_from_idle_history(self, observer):
        """Feed a completed turn's history → observer lands on IDLE."""
        history = [
            user_message(),
            agent_message(),
            turn_completed(),
        ]
        observer.orient_from_history(history)
        assert observer.state == ObserverState.IDLE

    def test_orient_from_busy_history(self, observer):
        """Feed a partial turn's history → observer lands on BUSY."""
        history = [
            user_message(),
            agent_message(),
            tool_call("t1"),
        ]
        observer.orient_from_history(history)
        assert observer.state == ObserverState.BUSY
        assert observer.has_pending_tool_calls

    def test_orient_uses_last_turn_only(self, observer):
        """Multiple turns in history — state reflects the last one."""
        history = [
            # Turn 1 (complete)
            user_message(),
            agent_message(),
            turn_completed(),
            # Turn 2 (in progress)
            user_message(),
            agent_thought(),
        ]
        observer.orient_from_history(history)
        assert observer.state == ObserverState.BUSY

    def test_orient_empty_history(self, observer):
        """Empty history → stays STARTING."""
        observer.orient_from_history([])
        assert observer.state == ObserverState.STARTING


# ============================================================================
# 12. No-visible-content retry sequence (from spec)
# ============================================================================

class TestRetrySequence:
    """
    The no_visible_content retry pattern:
    Binary completes a turn with only tool calls (no text output).
    System fires retry_state retrying (attempt 1..N).
    Observer should track this faithfully.
    """

    def test_no_visible_content_retry_sequence(self, observer):
        observer.process_event(user_message())
        observer.process_event(tool_call("t1", "read_file"))
        observer.process_event(tool_call_update("t1", "completed"))
        # No agent_message — triggers retry
        observer.process_event(retry_state("retrying", attempt=1, reason="no_visible_content"))
        assert observer.state == ObserverState.RETRYING
        assert observer.retry_attempt == 1

        observer.process_event(retry_state("retrying", attempt=2, reason="no_visible_content"))
        assert observer.retry_attempt == 2

        # Binary succeeds on retry
        observer.process_event(agent_message("here's the response"))
        assert observer.state == ObserverState.BUSY

        observer.process_event(turn_completed())
        assert observer.state == ObserverState.IDLE
