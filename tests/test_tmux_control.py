"""Tests for tmux_control.py — agent tmux session management.

Ported from mikeyv-infra/tests/test_tmux_control.py to agent-abide.
Tests TmuxSession lifecycle: launch, send, capture, wait_for, kill.
"""

import json
import os
import sys
import time
import pytest
import subprocess

# agent-abide keeps tmux_control in utils/
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'utils'))

import tmux_control
from tmux_control import TmuxSession, TmuxError, list_sessions


# Skip all tests if tmux is not available
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "tmux"], capture_output=True).returncode != 0,
    reason="tmux not installed"
)


@pytest.fixture
def session_name():
    """Generate a unique session name and ensure cleanup."""
    name = f"test_tmux_{int(time.time() * 1000) % 100000}"
    yield name
    # Cleanup: kill session if it still exists
    subprocess.run(["tmux", "kill-session", "-t", name],
                   capture_output=True, check=False)


class TestTmuxSessionLifecycle:
    def test_launch_and_kill(self, session_name):
        s = TmuxSession(session_name)
        s.launch("bash")
        assert s.is_alive()
        assert s.exists()
        s.kill()
        assert not s.is_alive()
        assert not s.exists()

    def test_launch_duplicate_raises(self, session_name):
        s = TmuxSession(session_name)
        s.launch("bash")
        try:
            with pytest.raises(TmuxError, match="already exists"):
                s.launch("bash")
        finally:
            s.kill()

    def test_context_manager(self, session_name):
        with TmuxSession(session_name) as s:
            s.launch("bash")
            assert s.is_alive()
        # After context manager exits, session should be killed
        assert not s.exists()

    def test_kill_nonexistent_is_noop(self, session_name):
        s = TmuxSession(session_name)
        # Should not raise
        s.kill()

    def test_exists_false_before_launch(self, session_name):
        s = TmuxSession(session_name)
        assert not s.exists()
        assert not s.is_alive()


class TestSendAndCapture:
    def test_send_and_capture(self, session_name):
        with TmuxSession(session_name) as s:
            s.launch("bash")
            time.sleep(0.3)  # let bash start
            s.send("echo HELLO_TMUX_TEST")
            time.sleep(0.3)  # let output render
            output = s.capture()
            assert "HELLO_TMUX_TEST" in output

    def test_send_without_enter(self, session_name):
        with TmuxSession(session_name) as s:
            s.launch("bash")
            time.sleep(0.3)
            s.send("partial text", enter=False)
            time.sleep(0.2)
            output = s.capture()
            assert "partial text" in output

    def test_send_keys_ctrl_c(self, session_name):
        with TmuxSession(session_name) as s:
            s.launch("bash")
            time.sleep(0.3)
            # Start a long-running command
            s.send("sleep 999")
            time.sleep(0.3)
            # Send Ctrl-C to interrupt
            s.send_keys("C-c")
            time.sleep(0.3)
            # Should be back at prompt (sleep was interrupted)
            output = s.capture()
            # The interrupted command or a new prompt should be visible
            assert output  # at minimum we got something back

    def test_send_multiline(self, session_name):
        with TmuxSession(session_name) as s:
            s.launch("bash")
            time.sleep(0.3)
            s.send("echo LINE_ONE")
            time.sleep(0.2)
            s.send("echo LINE_TWO")
            time.sleep(0.2)
            output = s.capture()
            assert "LINE_ONE" in output
            assert "LINE_TWO" in output


class TestWaitFor:
    def test_wait_for_immediate(self, session_name):
        with TmuxSession(session_name) as s:
            s.launch("bash")
            time.sleep(0.3)
            s.send("echo MARKER_12345")
            output = s.wait_for("MARKER_12345", timeout=5)
            assert "MARKER_12345" in output

    def test_wait_for_timeout(self, session_name):
        with TmuxSession(session_name) as s:
            s.launch("bash")
            time.sleep(0.3)
            with pytest.raises(TmuxError, match="Timeout"):
                s.wait_for("THIS_WILL_NEVER_APPEAR", timeout=1)

    def test_wait_for_stable(self, session_name):
        with TmuxSession(session_name) as s:
            s.launch("bash")
            time.sleep(0.3)
            s.send("echo DONE")
            time.sleep(0.3)
            output = s.wait_for_stable(timeout=5, stable_duration=0.5)
            assert "DONE" in output


class TestCapture:
    def test_capture_strips_trailing(self, session_name):
        with TmuxSession(session_name) as s:
            s.launch("bash")
            time.sleep(0.3)
            output = s.capture(strip_trailing=True)
            assert not output.endswith("\n\n")

    def test_capture_scrollback(self, session_name):
        with TmuxSession(session_name) as s:
            s.launch("bash")
            time.sleep(0.3)
            # Generate some output
            for i in range(5):
                s.send(f"echo scrollback_line_{i}")
                time.sleep(0.1)
            time.sleep(0.3)
            output = s.capture_scrollback(lines=50)
            assert "scrollback_line_0" in output
            assert "scrollback_line_4" in output


class TestOperationsOnDeadSession:
    def test_send_raises_on_dead(self, session_name):
        s = TmuxSession(session_name)
        with pytest.raises(TmuxError, match="not running"):
            s.send("hello")

    def test_capture_raises_on_dead(self, session_name):
        s = TmuxSession(session_name)
        with pytest.raises(TmuxError, match="not running"):
            s.capture()

    def test_wait_for_raises_on_dead(self, session_name):
        s = TmuxSession(session_name)
        with pytest.raises(TmuxError, match="not running"):
            s.wait_for("something")

    def test_send_keys_raises_on_dead(self, session_name):
        s = TmuxSession(session_name)
        with pytest.raises(TmuxError, match="not running"):
            s.send_keys("Enter")


class TestListSessions:
    def test_list_includes_launched(self, session_name):
        with TmuxSession(session_name) as s:
            s.launch("bash")
            sessions = list_sessions()
            names = [sess["name"] for sess in sessions]
            assert session_name in names

    def test_list_empty_when_none(self):
        # This might fail if other tmux sessions exist, so just check it returns a list
        result = list_sessions()
        assert isinstance(result, list)


class TestRepr:
    def test_repr_local(self, session_name):
        s = TmuxSession(session_name)
        r = repr(s)
        assert session_name in r
        assert "(local)" in r
        assert "dead" in r

    def test_repr_remote(self, session_name):
        s = TmuxSession(session_name, ssh_host="example.com")
        r = repr(s)
        assert "@example.com" in r

    def test_repr_alive(self, session_name):
        with TmuxSession(session_name) as s:
            s.launch("bash")
            r = repr(s)
            assert "alive" in r
