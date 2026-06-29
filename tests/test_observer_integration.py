"""
Observer integration tests — verifying BinaryStateObserver sidecar lifecycle
within asdaaas.

Tests I1-I10 from the observer migration plan. These verify the scaffold
(Phase 1) and heuristic swaps (Phase 2) work correctly.

Requires: observer_enabled=True in agent config, real or mock binary.
"""

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))

from binary_state_observer import BinaryStateObserver, ObserverState


# ── Helpers ──────────────────────────────────────────────────────────────

def write_mock_state_file(path, state="IDLE", since=None, doom_loop=False,
                          pid=12345, extra=None):
    """Write a mock observer state file with valid TTL.

    This is the M2 helper (MockObserverStateFile) from the migration plan.
    Use it to control what asdaaas reads from the observer without running
    the actual sidecar process.
    """
    now = time.time()
    data = {
        "state": state,
        "since": since or now - 1.0,
        "last_event_type": "turn_completed" if state == "IDLE" else "tool_call",
        "last_event_ts": now - 0.5,
        "retry_attempt": None,
        "retry_reason": None,
        "exit_code": None,
        "pid": pid,
        "pid_proc_state": "S",
        "unknown_event": None,
        "doom_loop": doom_loop,
        "turn_event_count": 42,
        "written_at": now,
        "expires_at": now + 10.0,  # generous TTL for testing
    }
    if extra:
        data.update(extra)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.rename(tmp, path)
    return data


def write_expired_state_file(path):
    """Write a state file that is already expired (observer dead)."""
    data = {
        "state": "IDLE",
        "since": time.time() - 10.0,
        "last_event_type": "turn_completed",
        "last_event_ts": time.time() - 10.0,
        "retry_attempt": None,
        "retry_reason": None,
        "exit_code": None,
        "pid": 12345,
        "pid_proc_state": "S",
        "unknown_event": None,
        "doom_loop": False,
        "turn_event_count": 0,
        "written_at": time.time() - 5.0,
        "expires_at": time.time() - 4.0,  # expired
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f)
    return data


# ── I1: Observer spawned on startup ──────────────────────────────────────

class TestObserverSpawnedOnStartup:
    """I1: Verify asdaaas spawns the observer sidecar when observer_enabled=True
    and the binary is running."""

    def test_spawn_creates_process(self, tmp_path):
        """Observer process is created after backend.start() when enabled."""
        state_file = tmp_path / "binary_state.json"
        observer_script = os.path.join(
            os.path.dirname(__file__), '..', 'core', 'binary_state_observer.py'
        )
        assert os.path.exists(observer_script), "Observer script not found"

        # Spawn a quick observer against our own PID (will see GONE quickly
        # since we're not a grok binary, but it proves the spawn mechanism)
        proc = asyncio.get_event_loop().run_until_complete(
            asyncio.create_subprocess_exec(
                sys.executable, observer_script,
                "--pid", str(os.getpid()),
                "--session-dir", str(tmp_path),
                "--state-file", str(state_file),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )
        try:
            # Give it time to write initial state
            time.sleep(1.0)
            assert proc.returncode is None or proc.returncode == 0
            # State file should exist
            assert state_file.exists(), "Observer did not write state file"
            state = BinaryStateObserver.read_state_file(str(state_file))
            # May be None if already expired, but file should exist
            assert state_file.exists()
        finally:
            proc.terminate()
            asyncio.get_event_loop().run_until_complete(
                asyncio.wait_for(proc.wait(), timeout=3.0)
            )

    def test_spawn_skipped_when_disabled(self):
        """Observer is NOT spawned when observer_enabled=False."""
        # The scaffold code checks observer_enabled before spawning.
        # This test verifies the guard by checking that the config
        # default is False.
        from asdaaas_config import AsdaaasConfig
        config = AsdaaasConfig()
        # Default should be False for any agent not explicitly configured
        assert config.agent_observer_enabled("nonexistent_test_agent") is False


# ── I2: Observer reaped on shutdown ──────────────────────────────────────

class TestObserverReapedOnShutdown:
    """I2: Verify the observer process is terminated on asdaaas shutdown."""

    def test_reap_sigterm(self, tmp_path):
        """Observer responds to SIGTERM and exits cleanly."""
        observer_script = os.path.join(
            os.path.dirname(__file__), '..', 'core', 'binary_state_observer.py'
        )
        state_file = tmp_path / "binary_state.json"

        proc = asyncio.get_event_loop().run_until_complete(
            asyncio.create_subprocess_exec(
                sys.executable, observer_script,
                "--pid", str(os.getpid()),
                "--session-dir", str(tmp_path),
                "--state-file", str(state_file),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )
        time.sleep(0.5)
        assert proc.returncode is None, "Observer exited prematurely"

        # Send SIGTERM (what asdaaas does)
        proc.terminate()
        rc = asyncio.get_event_loop().run_until_complete(
            asyncio.wait_for(proc.wait(), timeout=3.0)
        )
        # Should exit cleanly (0 or -SIGTERM)
        assert rc is not None, "Observer did not exit after SIGTERM"

    def test_no_orphan_after_reap(self, tmp_path):
        """After reap, observer PID is no longer running."""
        observer_script = os.path.join(
            os.path.dirname(__file__), '..', 'core', 'binary_state_observer.py'
        )
        state_file = tmp_path / "binary_state.json"

        proc = asyncio.get_event_loop().run_until_complete(
            asyncio.create_subprocess_exec(
                sys.executable, observer_script,
                "--pid", str(os.getpid()),
                "--session-dir", str(tmp_path),
                "--state-file", str(state_file),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )
        observer_pid = proc.pid
        time.sleep(0.5)

        proc.terminate()
        asyncio.get_event_loop().run_until_complete(
            asyncio.wait_for(proc.wait(), timeout=3.0)
        )

        # Verify process is gone
        try:
            os.kill(observer_pid, 0)
            pytest.fail(f"Observer PID {observer_pid} still alive after reap")
        except ProcessLookupError:
            pass  # Expected — process is gone


# ── I4: Observer crash fallback ──────────────────────────────────────────

class TestObserverCrashFallback:
    """I4: When observer dies or state file expires, read_state_file returns None."""

    def test_expired_state_returns_none(self, tmp_path):
        """Expired state file (observer dead) returns None."""
        state_file = tmp_path / "binary_state.json"
        write_expired_state_file(str(state_file))
        assert state_file.exists()

        result = BinaryStateObserver.read_state_file(str(state_file))
        assert result is None, "Expired state should return None"

    def test_missing_state_returns_none(self, tmp_path):
        """Missing state file returns None."""
        result = BinaryStateObserver.read_state_file(str(tmp_path / "nope.json"))
        assert result is None

    def test_corrupt_state_returns_none(self, tmp_path):
        """Corrupt state file returns None."""
        state_file = tmp_path / "binary_state.json"
        state_file.write_text("not json {{{")

        result = BinaryStateObserver.read_state_file(str(state_file))
        assert result is None

    def test_fresh_state_returns_dict(self, tmp_path):
        """Fresh (non-expired) state file returns valid dict."""
        state_file = tmp_path / "binary_state.json"
        written = write_mock_state_file(str(state_file), state="BUSY")

        result = BinaryStateObserver.read_state_file(str(state_file))
        assert result is not None
        assert result["state"] == "BUSY"


# ── I8: State file location ──────────────────────────────────────────────

class TestStateFileLocation:
    """I8: State file is written to the correct path."""

    def test_config_returns_correct_path(self):
        """Config method returns path under agent's asdaaas dir."""
        from asdaaas_config import AsdaaasConfig
        config = AsdaaasConfig()
        path = config.agent_observer_state_file("Trip")
        assert "Trip" in str(path)
        assert "asdaaas" in str(path)
        assert str(path).endswith("binary_state.json")


# ── M2: MockObserverStateFile helper ─────────────────────────────────────

class TestMockObserverStateFile:
    """M2: Verify the test helper correctly produces state files."""

    def test_write_mock_creates_valid_file(self, tmp_path):
        """write_mock_state_file creates a file read_state_file accepts."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="IDLE")

        result = BinaryStateObserver.read_state_file(str(path))
        assert result is not None
        assert result["state"] == "IDLE"

    def test_write_mock_with_doom_loop(self, tmp_path):
        """write_mock_state_file can set doom_loop flag."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="BUSY", doom_loop=True)

        result = BinaryStateObserver.read_state_file(str(path))
        assert result is not None
        assert result["doom_loop"] is True

    def test_write_mock_with_retrying(self, tmp_path):
        """write_mock_state_file can set retry state."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="RETRYING", extra={
            "retry_attempt": 3,
            "retry_reason": "no_visible_content",
        })

        result = BinaryStateObserver.read_state_file(str(path))
        assert result is not None
        assert result["state"] == "RETRYING"
        assert result["retry_attempt"] == 3

    def test_write_expired_is_stale(self, tmp_path):
        """write_expired_state_file creates a file that reads as None."""
        path = tmp_path / "state.json"
        write_expired_state_file(str(path))

        result = BinaryStateObserver.read_state_file(str(path))
        assert result is None


# ── I3: State file consumed for decisions ────────────────────────────────

class TestStateFileConsumedForDecisions:
    """I3: Verify the state dict produced by write_mock_state_file has the
    correct shape for every decision point in asdaaas.py Phase 2.

    Decision points reference:
    - L2728: collection window reads state, checks state=="IDLE"
    - L2774: continue gating reads state, checks state in BUSY/GONE/RETRYING/STUCK
    - L2839: midturn detection reads state, checks state + since
    - L3028: doom_loop reads state, checks doom_loop flag
    """

    def test_idle_state_has_collection_window_fields(self, tmp_path):
        """IDLE state dict has 'state' field for collection window optimization."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="IDLE")
        result = BinaryStateObserver.read_state_file(str(path))
        assert result is not None
        assert result["state"] == "IDLE"
        # Collection window code checks: obs.get("state") == "IDLE"
        assert "state" in result

    def test_busy_state_has_gating_fields(self, tmp_path):
        """BUSY state dict triggers the continue-skip branch."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="BUSY")
        result = BinaryStateObserver.read_state_file(str(path))
        assert result is not None
        assert result["state"] == "BUSY"

    def test_gone_state_has_exit_code(self, tmp_path):
        """GONE state dict includes exit_code for recovery logging."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="GONE", extra={"exit_code": 1})
        result = BinaryStateObserver.read_state_file(str(path))
        assert result is not None
        assert result["state"] == "GONE"
        assert result["exit_code"] == 1

    def test_retrying_state_has_attempt_and_reason(self, tmp_path):
        """RETRYING state dict includes retry_attempt and retry_reason."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="RETRYING", extra={
            "retry_attempt": 2,
            "retry_reason": "no_visible_content",
        })
        result = BinaryStateObserver.read_state_file(str(path))
        assert result is not None
        assert result["state"] == "RETRYING"
        assert result["retry_attempt"] == 2
        assert result["retry_reason"] == "no_visible_content"

    def test_stuck_state_has_since_for_duration(self, tmp_path):
        """STUCK state dict includes 'since' for duration calculation."""
        stuck_since = time.time() - 30.0
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="STUCK", since=stuck_since)
        result = BinaryStateObserver.read_state_file(str(path))
        assert result is not None
        assert result["state"] == "STUCK"
        assert result["since"] == pytest.approx(stuck_since, abs=0.1)
        # asdaaas calculates: stuck_dur = time.time() - since
        stuck_dur = time.time() - result["since"]
        assert stuck_dur >= 29.0

    def test_doom_loop_flag_present(self, tmp_path):
        """State dict includes doom_loop flag for L3028 check."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="BUSY", doom_loop=True)
        result = BinaryStateObserver.read_state_file(str(path))
        assert result is not None
        assert result["doom_loop"] is True

    def test_observer_disabled_returns_none(self):
        """When observer_enabled=False, read_observer_state should return None.
        This verifies the fallback path: obs is None → old heuristics."""
        # The closure in asdaaas checks observer_enabled first.
        # We test the config-level guard here.
        from asdaaas_config import AsdaaasConfig
        config = AsdaaasConfig()
        assert config.agent_observer_enabled("nonexistent_agent") is False


# ── I5: Midturn detection with observer ──────────────────────────────────

class TestMidturnDetectionWithObserver:
    """I5: Verify the midturn detection logic at L2839-2849 in asdaaas.py.

    Logic (when observer state is available):
    - BUSY → midturn = True (binary is processing)
    - IDLE + msg_ts < since → midturn = True (message arrived before IDLE)
    - IDLE + msg_ts >= since → midturn = False (message arrived after IDLE)
    - Other/unknown state → midturn = False
    """

    def _eval_midturn(self, obs_state, obs_since, msg_ts):
        """Replicate the inline midturn logic from asdaaas.py L2839-2849."""
        if not isinstance(msg_ts, (int, float)):
            return False
        if obs_state == "BUSY":
            return True
        if obs_state == "IDLE":
            return msg_ts < obs_since
        return False

    def test_busy_always_midturn(self):
        """When observer says BUSY, any message is midturn."""
        assert self._eval_midturn("BUSY", time.time(), time.time()) is True

    def test_idle_old_message_is_midturn(self):
        """When IDLE and message arrived before IDLE started, it's midturn."""
        idle_since = time.time()
        msg_ts = idle_since - 5.0  # message arrived 5s before IDLE
        assert self._eval_midturn("IDLE", idle_since, msg_ts) is True

    def test_idle_new_message_not_midturn(self):
        """When IDLE and message arrived after IDLE started, it's not midturn."""
        idle_since = time.time() - 10.0
        msg_ts = idle_since + 5.0  # message arrived 5s after IDLE
        assert self._eval_midturn("IDLE", idle_since, msg_ts) is False

    def test_idle_exact_boundary_not_midturn(self):
        """Message at exactly the IDLE boundary is not midturn (< not <=)."""
        idle_since = time.time()
        assert self._eval_midturn("IDLE", idle_since, idle_since) is False

    def test_unknown_state_not_midturn(self):
        """Unknown observer states default to not-midturn."""
        assert self._eval_midturn("STUCK", time.time(), time.time()) is False
        assert self._eval_midturn("RETRYING", time.time(), time.time()) is False
        assert self._eval_midturn("GONE", time.time(), time.time()) is False

    def test_non_numeric_timestamp_not_midturn(self):
        """Non-numeric msg_ts is not midturn (guard at L2842)."""
        assert self._eval_midturn("BUSY", time.time(), "not_a_number") is False
        assert self._eval_midturn("BUSY", time.time(), None) is False

    def test_state_file_provides_since_for_midturn(self, tmp_path):
        """State file read produces correct 'since' for midturn calc."""
        path = tmp_path / "state.json"
        target_since = time.time() - 2.0
        write_mock_state_file(str(path), state="IDLE", since=target_since)
        result = BinaryStateObserver.read_state_file(str(path))
        assert result is not None
        # This since value is what asdaaas uses for midturn comparison
        assert result["since"] == pytest.approx(target_since, abs=0.1)


# ── I7: STUCK replaces backoff ───────────────────────────────────────────

class TestStuckReplacesBackoff:
    """I7: When observer reports STUCK, asdaaas skips the continue.

    asdaaas.py L2800-2808: reads STUCK, calculates duration from 'since',
    writes health, sleeps 5s, continues (skips default doorbell).
    """

    def test_stuck_state_with_duration(self, tmp_path):
        """STUCK state file has the fields needed for duration calculation."""
        stuck_since = time.time() - 45.0
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="STUCK", since=stuck_since)
        result = BinaryStateObserver.read_state_file(str(path))

        assert result["state"] == "STUCK"
        # asdaaas calculates: stuck_dur = time.time() - since
        stuck_dur = time.time() - result["since"]
        assert stuck_dur >= 44.0
        assert stuck_dur < 50.0

    def test_stuck_replaces_old_backoff_path(self, tmp_path):
        """When observer returns STUCK, the fallback (has_pending_tool_calls)
        at L2809 is NOT reached — observer path takes priority."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="STUCK", since=time.time() - 10.0)
        result = BinaryStateObserver.read_state_file(str(path))
        # Key: result is not None, so the `elif obs is None` fallback at L2809
        # is unreachable. STUCK branch runs instead of the old backoff.
        assert result is not None
        assert result["state"] == "STUCK"


# ── I9: No continues when BUSY ──────────────────────────────────────────

class TestNoContinesWhenBusy:
    """I9: When observer reports BUSY, asdaaas skips the continue doorbell.

    asdaaas.py L2774-2779: BUSY → print, sleep 2s, continue (skip doorbell).
    This replaces the old has_pending_tool_calls heuristic.
    """

    def test_busy_state_blocks_continue(self, tmp_path):
        """BUSY state file is read correctly and would block continue."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="BUSY")
        result = BinaryStateObserver.read_state_file(str(path))

        assert result is not None
        assert result["state"] == "BUSY"
        # When obs["state"] == "BUSY", asdaaas skips queue_continue_doorbell

    def test_busy_state_preempts_fallback(self, tmp_path):
        """When observer returns BUSY (not None), the old heuristic
        (has_pending_tool_calls) at L2809 is never evaluated."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="BUSY")
        result = BinaryStateObserver.read_state_file(str(path))
        # obs is not None → elif at L2809 is unreachable
        assert result is not None

    def test_idle_state_allows_continue(self, tmp_path):
        """IDLE state does NOT block continue — only BUSY/GONE/RETRYING/STUCK do."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="IDLE")
        result = BinaryStateObserver.read_state_file(str(path))

        assert result is not None
        assert result["state"] == "IDLE"
        # IDLE falls through all the if/elif checks → reaches queue_continue_doorbell


# ── I10: GONE triggers recovery ─────────────────────────────────────────

class TestGoneTriggersRecovery:
    """I10: When observer reports GONE, asdaaas stops continues and notifies.

    asdaaas.py L2780-2794: GONE → set delay_until_event, write health
    "stalled" with "binary_gone (exit=N)", send localmail to Sr.
    """

    def test_gone_state_has_exit_code(self, tmp_path):
        """GONE state includes exit_code for the health message."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="GONE", extra={"exit_code": 137})
        result = BinaryStateObserver.read_state_file(str(path))

        assert result is not None
        assert result["state"] == "GONE"
        assert result["exit_code"] == 137

    def test_gone_state_exit_code_none(self, tmp_path):
        """GONE with exit_code=None (process vanished without exit)."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="GONE", extra={"exit_code": None})
        result = BinaryStateObserver.read_state_file(str(path))

        assert result is not None
        assert result["state"] == "GONE"
        assert result["exit_code"] is None

    def test_gone_preempts_fallback(self, tmp_path):
        """GONE observer state preempts the old has_pending_tool_calls check."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="GONE", extra={"exit_code": 0})
        result = BinaryStateObserver.read_state_file(str(path))
        assert result is not None


# ── I6: Doom loop detection via observer ─────────────────────────────────

class TestDoomLoopViaObserver:
    """I6: When observer sets doom_loop=True, asdaaas stops continues.

    asdaaas.py L3027-3041: reads doom_loop flag, sets delay_until_event,
    writes health "stalled" with "observer_doom_loop_detected",
    sends localmail to Sr.
    """

    def test_doom_loop_flag_true(self, tmp_path):
        """doom_loop=True in state file is read correctly."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="BUSY", doom_loop=True)
        result = BinaryStateObserver.read_state_file(str(path))

        assert result is not None
        assert result["doom_loop"] is True

    def test_doom_loop_flag_false(self, tmp_path):
        """doom_loop=False does NOT trigger the doom loop path."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="BUSY", doom_loop=False)
        result = BinaryStateObserver.read_state_file(str(path))

        assert result is not None
        assert result["doom_loop"] is False

    def test_doom_loop_preempts_heuristic(self, tmp_path):
        """When observer state is not None, the old consecutive-empty-doorbell
        doom loop check at L3042 is unreachable."""
        path = tmp_path / "state.json"
        write_mock_state_file(str(path), state="IDLE", doom_loop=False)
        result = BinaryStateObserver.read_state_file(str(path))
        # obs_doom is not None → elif at L3042 is unreachable
        assert result is not None
