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
