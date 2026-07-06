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
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from binary_state_observer import (
    BinaryStateObserver, ObserverState, UpdatesJSONLTailer,
    ObserverService, load_known_types, load_silence_windows,
    STATE_TTL,
)


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


def tool_call(tool_id="t1", tool_name="run_terminal_command", raw_input=None, kind=None):
    extra = {"toolCallId": tool_id, "title": tool_name}
    if raw_input:
        extra["rawInput"] = raw_input
    if kind:
        extra["_meta"] = {"x.ai/tool": {"kind": kind}}
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


# ============================================================================
# 13. UpdatesJSONLTailer
# ============================================================================

class TestTailer:
    """File tailing for updates.jsonl: reads new lines, handles truncation."""

    def test_read_new_lines_from_file(self, tmp_path):
        f = tmp_path / "updates.jsonl"
        f.write_text(json.dumps(user_message()) + "\n")

        tailer = UpdatesJSONLTailer(str(f))
        lines = tailer.read_new_lines()
        assert len(lines) == 1
        assert "user_message_chunk" in lines[0]
        tailer.close()

    def test_incremental_reads(self, tmp_path):
        f = tmp_path / "updates.jsonl"
        f.write_text(json.dumps(user_message()) + "\n")

        tailer = UpdatesJSONLTailer(str(f))
        lines1 = tailer.read_new_lines()
        assert len(lines1) == 1

        # Append more
        with open(f, "a") as fh:
            fh.write(json.dumps(agent_message()) + "\n")
            fh.write(json.dumps(turn_completed()) + "\n")

        lines2 = tailer.read_new_lines()
        assert len(lines2) == 2
        tailer.close()

    def test_no_file_returns_empty(self, tmp_path):
        tailer = UpdatesJSONLTailer(str(tmp_path / "nonexistent.jsonl"))
        assert tailer.read_new_lines() == []
        tailer.close()

    def test_partial_line_not_returned(self, tmp_path):
        """Incomplete line (no trailing newline) waits for completion."""
        f = tmp_path / "updates.jsonl"
        f.write_text('{"partial": true')  # no newline

        tailer = UpdatesJSONLTailer(str(f))
        lines = tailer.read_new_lines()
        assert len(lines) == 0  # not returned yet

        # Complete the line
        with open(f, "a") as fh:
            fh.write('}\n')

        lines = tailer.read_new_lines()
        assert len(lines) == 1
        tailer.close()

    def test_read_tail_lines(self, tmp_path):
        f = tmp_path / "updates.jsonl"
        with open(f, "w") as fh:
            for i in range(100):
                fh.write(json.dumps({"line": i}) + "\n")

        tailer = UpdatesJSONLTailer(str(f))
        tail = tailer.read_tail_lines(5)
        assert len(tail) == 5
        assert json.loads(tail[-1])["line"] == 99
        tailer.close()

    def test_seek_to_end(self, tmp_path):
        f = tmp_path / "updates.jsonl"
        f.write_text(json.dumps(user_message()) + "\n")

        tailer = UpdatesJSONLTailer(str(f))
        tailer.seek_to_end()

        # Old content not returned
        assert tailer.read_new_lines() == []

        # New content is
        with open(f, "a") as fh:
            fh.write(json.dumps(agent_message()) + "\n")
        lines = tailer.read_new_lines()
        assert len(lines) == 1
        tailer.close()

    def test_file_truncation_detected(self, tmp_path):
        """If the file is replaced/truncated, tailer resets."""
        f = tmp_path / "updates.jsonl"
        f.write_text(json.dumps(user_message()) + "\n" * 50)

        tailer = UpdatesJSONLTailer(str(f))
        tailer.read_new_lines()  # read all

        # Truncate (new session)
        f.write_text(json.dumps(agent_message()) + "\n")

        lines = tailer.read_new_lines()
        assert len(lines) == 1
        assert "agent_message_chunk" in lines[0]
        tailer.close()


# ============================================================================
# 14. State file TTL
# ============================================================================

class TestStateFileTTL:
    """State file expires after STATE_TTL seconds."""

    def test_read_fresh_state_file(self, tmp_path, observer):
        path = str(tmp_path / "state.json")
        observer.process_event(user_message())
        observer.write_state_file(path)

        state = BinaryStateObserver.read_state_file(path)
        assert state is not None
        assert state["state"] == "BUSY"

    def test_expired_state_file_returns_none(self, tmp_path, observer):
        path = str(tmp_path / "state.json")
        observer.process_event(user_message())
        observer.write_state_file(path)

        # Backdate the expiration
        with open(path) as f:
            data = json.load(f)
        data["expires_at"] = time.time() - 10  # expired 10s ago
        with open(path, "w") as f:
            json.dump(data, f)

        state = BinaryStateObserver.read_state_file(path)
        assert state is None

    def test_missing_state_file_returns_none(self, tmp_path):
        state = BinaryStateObserver.read_state_file(str(tmp_path / "nope.json"))
        assert state is None

    def test_corrupt_state_file_returns_none(self, tmp_path):
        path = str(tmp_path / "state.json")
        with open(path, "w") as f:
            f.write("{corrupt json")

        state = BinaryStateObserver.read_state_file(path)
        assert state is None

    def test_state_dict_has_written_at_and_expires_at(self, observer):
        observer.process_event(user_message())
        d = observer.state_dict()
        assert "written_at" in d
        assert "expires_at" in d
        assert d["expires_at"] > d["written_at"]
        assert d["expires_at"] - d["written_at"] == pytest.approx(STATE_TTL, abs=0.01)


# ============================================================================
# 15. Data file loading
# ============================================================================

class TestDataLoading:
    """Load known types and silence windows from data files."""

    def test_load_known_types(self):
        data_dir = str(Path(__file__).resolve().parent.parent / "core" / "observer_data")
        types = load_known_types(data_dir)
        assert "user_message_chunk" in types
        assert "turn_completed" in types
        assert "tool_call" in types
        assert len(types) >= 51  # spec says 51+, Sr shipped 53

    def test_load_silence_windows(self):
        data_dir = str(Path(__file__).resolve().parent.parent / "core" / "observer_data")
        by_tool, by_event, default, p95 = load_silence_windows(data_dir)
        assert "read_file" in by_tool
        assert by_tool["read_file"] > 0
        assert "tool_call" in by_event
        assert default > 0
        assert p95 > 0

    def test_silence_windows_tool_hierarchy(self):
        """Fast tools should have shorter windows than slow tools."""
        data_dir = str(Path(__file__).resolve().parent.parent / "core" / "observer_data")
        by_tool, _, _, _ = load_silence_windows(data_dir)
        assert by_tool.get("read_file", 10) < by_tool.get("run_terminal_command", 120)
        assert by_tool.get("read_file", 10) < by_tool.get("spawn_subagent", 600)


# ============================================================================
# 16. Event silence windows (Sr's addition)
# ============================================================================

class TestEventSilenceWindows:
    """Per-event-type silence windows update expected_silence on each event."""

    def test_agent_message_sets_short_window(self):
        obs = BinaryStateObserver(
            pid=12345,
            known_types=set(KNOWN_TYPES),
            process_alive_fn=lambda pid: True,
            event_silence_windows={"agent_message_chunk": 5.0},
        )
        obs.process_event(user_message())
        obs.process_event(agent_message())
        assert obs._expected_silence == 5.0

    def test_tool_call_overrides_event_window(self):
        """tool_call uses _compute_expected_silence, not event window."""
        obs = BinaryStateObserver(
            pid=12345,
            known_types=set(KNOWN_TYPES),
            process_alive_fn=lambda pid: True,
            event_silence_windows={"tool_call": 5.0},
            silence_windows={"read_file": 10.0},
        )
        obs.process_event(user_message())
        obs.process_event(tool_call("t1", "read_file"))
        assert obs._expected_silence == 10.0  # per-tool, not per-event


# ============================================================================
# 17. ObserverService orientation
# ============================================================================

class TestServiceOrientation:
    """ObserverService orients from existing updates.jsonl tail."""

    def test_orient_from_completed_session(self, tmp_path):
        """Service orients to IDLE from a completed turn."""
        session_dir = str(tmp_path / "session")
        os.makedirs(session_dir)

        # Write a complete turn
        updates = tmp_path / "session" / "updates.jsonl"
        with open(updates, "w") as f:
            f.write(json.dumps(user_message()) + "\n")
            f.write(json.dumps(agent_message()) + "\n")
            f.write(json.dumps(turn_completed()) + "\n")

        data_dir = str(Path(__file__).resolve().parent.parent / "core" / "observer_data")
        state_file = str(tmp_path / "state.json")

        service = ObserverService(
            pid=os.getpid(),  # use our own PID (alive)
            session_dir=session_dir,
            state_file=state_file,
            data_dir=data_dir,
        )
        service.orient()

        assert service.observer.state == ObserverState.IDLE

        # State file was written
        state = BinaryStateObserver.read_state_file(state_file)
        assert state is not None
        assert state["state"] == "IDLE"

    def test_orient_empty_session(self, tmp_path):
        """No updates.jsonl → stays STARTING."""
        session_dir = str(tmp_path / "session")
        os.makedirs(session_dir)

        data_dir = str(Path(__file__).resolve().parent.parent / "core" / "observer_data")
        state_file = str(tmp_path / "state.json")

        service = ObserverService(
            pid=os.getpid(),
            session_dir=session_dir,
            state_file=state_file,
            data_dir=data_dir,
        )
        service.orient()

        assert service.observer.state == ObserverState.STARTING


# ============================================================================
# GATE state — interactive binary gates
# ============================================================================

class TestGateDetection:
    """GATE fires instead of STUCK when a pending tool is an interactive gate."""

    def test_exit_plan_tool_triggers_gate(self, observer):
        """exit_plan tool kind → GATE instead of STUCK on silence."""
        observer.process_event(user_message())
        observer.process_event(tool_call("t1", "exit_plan_mode", kind="exit_plan"))

        observer._last_event_ts = time.time() - 120
        observer.check_heartbeat()
        assert observer.state == ObserverState.GATE

    def test_ask_user_tool_triggers_gate(self, observer):
        """ask_user tool kind → GATE instead of STUCK on silence."""
        observer.process_event(user_message())
        observer.process_event(tool_call("t1", "ask_user_question", kind="ask_user"))

        observer._last_event_ts = time.time() - 120
        observer.check_heartbeat()
        assert observer.state == ObserverState.GATE

    def test_non_gate_tool_still_stuck(self, observer):
        """Regular tool kind → STUCK, not GATE."""
        observer.process_event(user_message())
        observer.process_event(tool_call("t1", "run_terminal_command", kind="bash"))

        observer._last_event_ts = time.time() - 120
        observer.check_heartbeat()
        assert observer.state == ObserverState.STUCK

    def test_unknown_kind_still_stuck(self, observer):
        """Tool with no kind metadata → STUCK, not GATE."""
        observer.process_event(user_message())
        observer.process_event(tool_call("t1", "run_terminal_command"))

        observer._last_event_ts = time.time() - 120
        observer.check_heartbeat()
        assert observer.state == ObserverState.STUCK

    def test_gate_clears_on_tool_complete(self, observer):
        """Tool completion clears GATE → back to BUSY."""
        observer.process_event(user_message())
        observer.process_event(tool_call("t1", "exit_plan_mode", kind="exit_plan"))

        observer._last_event_ts = time.time() - 120
        observer.check_heartbeat()
        assert observer.state == ObserverState.GATE

        observer.process_event(tool_call_update("t1", "completed"))
        observer.process_event(agent_message())
        assert observer.state == ObserverState.BUSY

    def test_mixed_tools_gate_wins(self, observer):
        """If any pending tool is a gate kind, report GATE not STUCK."""
        observer.process_event(user_message())
        observer.process_event(tool_call("t1", "run_terminal_command", kind="bash"))
        observer.process_event(tool_call("t2", "exit_plan_mode", kind="exit_plan"))

        observer._last_event_ts = time.time() - 120
        observer.check_heartbeat()
        assert observer.state == ObserverState.GATE

    def test_pending_tools_in_state_dict(self, observer):
        """state_dict includes pending_tools when non-empty."""
        observer.process_event(user_message())
        observer.process_event(tool_call("t1", "exit_plan_mode", kind="exit_plan"))

        state = observer.state_dict()
        assert state["pending_tools"] is not None
        assert state["pending_tools"]["t1"] == "exit_plan"

    def test_no_pending_tools_is_none(self, observer):
        """state_dict pending_tools is None when empty."""
        observer.process_event(user_message())
        observer.process_event(agent_message())

        state = observer.state_dict()
        assert state["pending_tools"] is None
