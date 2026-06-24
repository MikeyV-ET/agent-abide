"""Tests for remind_adapter.py — self-nudge control adapter.

Ported from mikeyv-infra/tests/test_remind_adapter.py to agent-abide.
"""

import json
import os
import time
import pytest
import sys
from pathlib import Path

# Add core and adapters directories to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapters"))

import remind_adapter
import adapter_api


# ============================================================================
# Fixture: hub_dir — temporary agent filesystem with monkeypatching
# ============================================================================

@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    """Create a temporary asdaaas directory structure and monkeypatch modules.

    Mirrors the agent-centric layout:
      tmp_path/agents/<AgentName>/asdaaas/doorbells/
      tmp_path/agents/<AgentName>/asdaaas/adapters/remind/{inbox,outbox}/

    Returns tmp_path/asdaaas (engine dir) for backward compat with test paths
    that navigate via hub_dir.parent / "agents".
    """
    hub = tmp_path / "asdaaas"
    agents_home = tmp_path / "agents"

    agents = ["Sr", "Jr", "Trip", "Q", "Cinco"]

    for agent in agents:
        agent_asdaaas = agents_home / agent / "asdaaas"
        (agent_asdaaas / "doorbells").mkdir(parents=True, exist_ok=True)
        (agent_asdaaas / "adapters" / "remind" / "inbox").mkdir(parents=True, exist_ok=True)
        (agent_asdaaas / "adapters" / "remind" / "outbox").mkdir(parents=True, exist_ok=True)

    (hub / "adapters").mkdir(parents=True, exist_ok=True)

    # Monkeypatch remind_adapter
    monkeypatch.setattr(remind_adapter, "HUB_DIR", hub)
    monkeypatch.setattr(remind_adapter, "AGENTS_DIR", hub / "agents")
    monkeypatch.setattr(remind_adapter, "AGENTS_HOME_DIR", agents_home)

    # Monkeypatch adapter_api
    monkeypatch.setattr(adapter_api, "HUB_DIR", hub)
    monkeypatch.setattr(adapter_api, "AGENTS_DIR", hub / "agents")
    monkeypatch.setattr(adapter_api, "AGENTS_HOME_DIR", agents_home)

    # Monkeypatch asdaaas (for integration tests)
    import asdaaas
    monkeypatch.setattr(asdaaas, "AGENTS_HOME_DIR", agents_home)

    return hub


# ============================================================================
# deliver_doorbell
# ============================================================================

class TestDeliverDoorbell:
    def test_creates_doorbell_file(self, hub_dir):
        remind_adapter.deliver_doorbell("Q", "Redirect gaze to pm:eric")
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1
        with open(bells[0]) as f:
            bell = json.load(f)
        assert bell["adapter"] == "remind"
        assert bell["text"] == "Redirect gaze to pm:eric"
        assert bell["priority"] == 1

    def test_custom_priority(self, hub_dir):
        remind_adapter.deliver_doorbell("Trip", "check later", priority=5)
        bells = list((hub_dir.parent / "agents" / "Trip" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1
        with open(bells[0]) as f:
            bell = json.load(f)
        assert bell["priority"] == 5

    def test_has_timestamp(self, hub_dir):
        remind_adapter.deliver_doorbell("Sr", "test")
        bells = list((hub_dir.parent / "agents" / "Sr" / "asdaaas" / "doorbells").glob("*.json"))
        with open(bells[0]) as f:
            bell = json.load(f)
        assert "ts" in bell

    def test_multiple_doorbells(self, hub_dir):
        remind_adapter.deliver_doorbell("Q", "first")
        remind_adapter.deliver_doorbell("Q", "second")
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 2


# ============================================================================
# TimerPool
# ============================================================================

class TestTimerPool:
    def test_immediate_delivery(self, hub_dir):
        timers = remind_adapter.TimerPool()
        timers.schedule("Q", "immediate nudge", delay=0)
        # Immediate means no timer thread
        assert timers.count == 0
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1

    def test_delayed_delivery(self, hub_dir):
        timers = remind_adapter.TimerPool()
        timers.schedule("Q", "delayed nudge", delay=0.2)
        assert timers.count == 1
        # Not delivered yet
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 0
        # Wait for delivery
        time.sleep(0.4)
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1
        with open(bells[0]) as f:
            bell = json.load(f)
        assert bell["text"] == "delayed nudge"

    def test_timer_cleans_up(self, hub_dir):
        timers = remind_adapter.TimerPool()
        timers.schedule("Q", "test", delay=0.1)
        assert timers.count == 1
        time.sleep(0.3)
        assert timers.count == 0

    def test_fractional_delay(self, hub_dir):
        timers = remind_adapter.TimerPool()
        timers.schedule("Q", "half second", delay=0.5)
        assert timers.count == 1
        time.sleep(0.3)
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 0  # not yet
        time.sleep(0.4)
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1

    def test_multiple_timers(self, hub_dir):
        timers = remind_adapter.TimerPool()
        timers.schedule("Q", "first", delay=0.1)
        timers.schedule("Q", "second", delay=0.2)
        assert timers.count == 2
        time.sleep(0.4)
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 2


# ============================================================================
# process_command
# ============================================================================

class TestProcessCommand:
    def test_remind_immediate(self, hub_dir):
        timers = remind_adapter.TimerPool()
        cmd = {"command": "remind", "delay": 0, "text": "redirect gaze"}
        remind_adapter.process_command(cmd, "Q", timers)
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1
        with open(bells[0]) as f:
            bell = json.load(f)
        assert bell["text"] == "redirect gaze"

    def test_remind_with_delay(self, hub_dir):
        timers = remind_adapter.TimerPool()
        cmd = {"command": "remind", "delay": 0.2, "text": "check trip"}
        remind_adapter.process_command(cmd, "Q", timers)
        assert timers.count == 1
        time.sleep(0.4)
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1

    def test_remind_custom_priority(self, hub_dir):
        timers = remind_adapter.TimerPool()
        cmd = {"command": "remind", "delay": 0, "text": "low priority", "priority": 5}
        remind_adapter.process_command(cmd, "Q", timers)
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        with open(bells[0]) as f:
            bell = json.load(f)
        assert bell["priority"] == 5

    def test_unknown_command(self, hub_dir):
        timers = remind_adapter.TimerPool()
        cmd = {"command": "explode", "text": "boom"}
        remind_adapter.process_command(cmd, "Q", timers)
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1
        with open(bells[0]) as f:
            bell = json.load(f)
        assert "error" in bell["text"].lower()
        assert "unknown" in bell["text"].lower()

    def test_missing_text(self, hub_dir):
        timers = remind_adapter.TimerPool()
        cmd = {"command": "remind", "delay": 0}
        remind_adapter.process_command(cmd, "Q", timers)
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        with open(bells[0]) as f:
            bell = json.load(f)
        assert "error" in bell["text"].lower()

    def test_invalid_delay(self, hub_dir):
        timers = remind_adapter.TimerPool()
        cmd = {"command": "remind", "delay": "banana", "text": "test"}
        remind_adapter.process_command(cmd, "Q", timers)
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        with open(bells[0]) as f:
            bell = json.load(f)
        assert "error" in bell["text"].lower()

    def test_negative_delay_treated_as_zero(self, hub_dir):
        timers = remind_adapter.TimerPool()
        cmd = {"command": "remind", "delay": -5, "text": "negative"}
        remind_adapter.process_command(cmd, "Q", timers)
        # Should deliver immediately (no timer)
        assert timers.count == 0
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1
        with open(bells[0]) as f:
            bell = json.load(f)
        assert bell["text"] == "negative"

    def test_default_delay_is_zero(self, hub_dir):
        timers = remind_adapter.TimerPool()
        cmd = {"command": "remind", "text": "no delay specified"}
        remind_adapter.process_command(cmd, "Q", timers)
        assert timers.count == 0  # immediate
        bells = list((hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells").glob("*.json"))
        assert len(bells) == 1


# ============================================================================
# Integration: doorbell format matches asdaaas expectations
# ============================================================================

class TestIntegration:
    def test_doorbell_readable_by_asdaaas(self, hub_dir):
        """Verify remind doorbells match the format asdaaas.format_doorbell expects."""
        import asdaaas
        remind_adapter.deliver_doorbell("Q", "redirect gaze to pm:eric")
        bells = asdaaas.poll_doorbells("Q")
        assert len(bells) == 1
        formatted = asdaaas.format_doorbell(bells[0])
        assert "[remind" in formatted
        assert "redirect gaze to pm:eric" in formatted

    def test_doorbell_priority_ordering(self, hub_dir):
        """Verify remind doorbells sort correctly with other doorbells."""
        import asdaaas
        # Write a low-priority heartbeat doorbell
        bell_dir = hub_dir.parent / "agents" / "Q" / "asdaaas" / "doorbells"
        hb = {"adapter": "heartbeat", "priority": 5, "text": "idle 15 min"}
        with open(bell_dir / "hb_001.json", "w") as f:
            json.dump(hb, f)
        # Write a high-priority remind doorbell
        remind_adapter.deliver_doorbell("Q", "redirect now", priority=1)
        bells = asdaaas.poll_doorbells("Q")
        assert len(bells) == 2
        assert bells[0]["adapter"] == "remind"  # priority 1 first
        assert bells[1]["adapter"] == "heartbeat"  # priority 5 second

    def test_agent_writes_command_via_adapter_api(self, hub_dir):
        """Simulate an agent writing a remind command via adapter_api."""
        cmd = json.dumps({"command": "remind", "delay": 0, "text": "continue with Eric"})
        adapter_api.write_to_adapter_inbox("remind", "Q", cmd, sender="Q")

        # Poll like the adapter would
        messages = adapter_api.poll_adapter_inbox("remind", "Q")
        assert len(messages) == 1

        parsed = json.loads(messages[0]["text"])
        assert parsed["command"] == "remind"
        assert parsed["text"] == "continue with Eric"
