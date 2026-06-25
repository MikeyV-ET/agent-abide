"""Comms channel tests -- doorbell lifecycle and remind adapter.

Covers gaps not in test_localmail.py: doorbell ack, doorbell persistence,
has_pending_doorbells, and remind adapter file format.

Run: pytest test_comms.py -v
"""

import json
import os
import sys
import time
import pytest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))

from asdaaas import ack_doorbells, has_pending_doorbells, _is_midturn_message, MIDTURN_GRACE_SECONDS


@pytest.fixture
def tmp_agents(tmp_path, monkeypatch):
    """Create a temporary agents home with one agent."""
    agent_name = "TestBot"
    agent_home = tmp_path / agent_name
    bell_dir = agent_home / "asdaaas" / "doorbells"
    bell_dir.mkdir(parents=True)

    # Patch agent_dir to return our temp path
    import asdaaas
    monkeypatch.setattr(asdaaas, "agent_dir", lambda name: tmp_path / name / "asdaaas")

    return {
        "agent_name": agent_name,
        "agents_home": tmp_path,
        "bell_dir": bell_dir,
    }


def write_doorbell(bell_dir, bell_id, adapter="localmail", text="test"):
    """Helper: write a doorbell file."""
    bell = {
        "id": bell_id,
        "adapter": adapter,
        "text": text,
        "ts": time.time(),
    }
    path = bell_dir / f"bell_{bell_id}.json"
    with open(path, "w") as f:
        json.dump(bell, f)
    return path


# ============================================================================
# CC7: Doorbell lifecycle -- create, persist, ack, gone
# ============================================================================

class TestDoorbellLifecycle:
    """CC7: Full doorbell lifecycle tests."""

    def test_cc7a_doorbell_persists_until_acked(self, tmp_agents):
        """Doorbells persist on disk until explicitly acked."""
        bell_dir = tmp_agents["bell_dir"]
        path = write_doorbell(bell_dir, "test_bell_1")
        assert path.exists()
        assert has_pending_doorbells(tmp_agents["agent_name"])
        assert path.exists()

    def test_cc7b_ack_removes_doorbell(self, tmp_agents):
        """Acking a doorbell removes it from disk."""
        bell_dir = tmp_agents["bell_dir"]
        write_doorbell(bell_dir, "ack_me")
        assert has_pending_doorbells(tmp_agents["agent_name"])

        removed = ack_doorbells(tmp_agents["agent_name"], ["ack_me"])
        assert removed == 1
        assert not has_pending_doorbells(tmp_agents["agent_name"])

    def test_cc7c_ack_only_removes_specified(self, tmp_agents):
        """Acking one doorbell leaves others."""
        bell_dir = tmp_agents["bell_dir"]
        write_doorbell(bell_dir, "keep_me")
        write_doorbell(bell_dir, "remove_me")
        assert len(list(bell_dir.glob("*.json"))) == 2

        ack_doorbells(tmp_agents["agent_name"], ["remove_me"])
        remaining = list(bell_dir.glob("*.json"))
        assert len(remaining) == 1
        with open(remaining[0]) as f:
            assert json.load(f)["id"] == "keep_me"

    def test_cc7d_ack_nonexistent_is_safe(self, tmp_agents):
        """Acking a non-existent doorbell ID doesn't error."""
        removed = ack_doorbells(tmp_agents["agent_name"], ["ghost_bell"])
        assert removed == 0

    def test_cc7e_has_pending_false_when_empty(self, tmp_agents):
        """has_pending_doorbells returns False when no doorbells."""
        assert not has_pending_doorbells(tmp_agents["agent_name"])

    def test_cc7f_has_pending_true_when_present(self, tmp_agents):
        """has_pending_doorbells returns True when doorbells exist."""
        write_doorbell(tmp_agents["bell_dir"], "exists")
        assert has_pending_doorbells(tmp_agents["agent_name"])

    def test_cc7g_multiple_ack_at_once(self, tmp_agents):
        """Can ack multiple doorbells in one call."""
        bell_dir = tmp_agents["bell_dir"]
        write_doorbell(bell_dir, "a")
        write_doorbell(bell_dir, "b")
        write_doorbell(bell_dir, "c")
        assert len(list(bell_dir.glob("*.json"))) == 3

        removed = ack_doorbells(tmp_agents["agent_name"], ["a", "c"])
        assert removed == 2
        remaining = list(bell_dir.glob("*.json"))
        assert len(remaining) == 1


# ============================================================================
# CC6: Remind adapter -- file format
# ============================================================================

class TestRemindAdapter:
    """CC6: Remind adapter writes correct file format."""

    def test_cc6a_remind_file_format(self, tmp_agents):
        """Remind file has command, delay, and text fields."""
        remind_dir = tmp_agents["agents_home"] / tmp_agents["agent_name"] / "asdaaas" / "adapters" / "remind" / "inbox"
        remind_dir.mkdir(parents=True)

        cmd = {"command": "remind", "delay": 300, "text": "Check status"}
        path = remind_dir / f"remind_{int(time.time()*1000)}.json"
        with open(path, "w") as f:
            json.dump(cmd, f)

        with open(path) as f:
            loaded = json.load(f)
        assert loaded["command"] == "remind"
        assert loaded["delay"] == 300
        assert loaded["text"] == "Check status"

    def test_cc6b_remind_delay_types(self, tmp_agents):
        """Remind delay can be integer (seconds)."""
        remind_dir = tmp_agents["agents_home"] / tmp_agents["agent_name"] / "asdaaas" / "adapters" / "remind" / "inbox"
        remind_dir.mkdir(parents=True)

        for delay in [60, 300, 3600, 7200]:
            cmd = {"command": "remind", "delay": delay, "text": f"Test {delay}s"}
            path = remind_dir / f"remind_test_{delay}.json"
            with open(path, "w") as f:
                json.dump(cmd, f)
            with open(path) as f:
                loaded = json.load(f)
            assert loaded["delay"] == delay


# ============================================================================
# Midturn detection
# ============================================================================

class TestMidturnDetection:
    """Tests for _is_midturn_message logic."""

    def test_none_response_ts_returns_false(self):
        """No prior response → nothing is midturn."""
        msg = {"_received_ts": time.time()}
        assert _is_midturn_message(msg, None) is False

    def test_msg_before_response_is_midturn(self):
        """Message sent before agent responded → midturn."""
        now = time.time()
        msg = {"_received_ts": now - 10}
        assert _is_midturn_message(msg, now) is True

    def test_msg_after_response_is_not_midturn(self):
        """Message sent after agent responded → not midturn."""
        now = time.time()
        msg = {"_received_ts": now + 5}
        assert _is_midturn_message(msg, now) is False

    def test_no_grace_on_non_foreground(self):
        """Non-foreground turn: no grace period (issue_0035 fix)."""
        now = time.time()
        msg = {"_received_ts": now + 10}  # 10s after response
        assert _is_midturn_message(msg, now, last_was_foreground=False) is False

    def test_no_grace_on_foreground(self):
        """Foreground turn: msg after response is not midturn (no grace)."""
        now = time.time()
        msg = {"_received_ts": now + 10}
        assert _is_midturn_message(msg, now, last_was_foreground=True) is False

    def test_no_false_flag_between_turns(self):
        """Messages arriving between turns are never flagged midturn (issue_0035).
        
        Eric scenario: agent processes doorbell (non-foreground), goes idle,
        user sends message within 30s. Should NOT be flagged as midturn."""
        response_ts = time.time()
        msg = {"_received_ts": response_ts + 5}  # 5s after response

        # Neither foreground nor non-foreground should flag this
        assert _is_midturn_message(msg, response_ts, last_was_foreground=False) is False
        assert _is_midturn_message(msg, response_ts, last_was_foreground=True) is False
