"""Tests for Phases 4.4, 5, 6.1-6.3, 7.1-7.2.

Tests extractable functions — no subprocess or async needed.

Ported from mikeyv-infra/tests/test_phases_4_7.py on 2026-06-24.
"""

import json
import os
import time
import pytest
from pathlib import Path
from unittest.mock import patch
import sys

# Add core and adapters to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapters"))

import asdaaas
import context_adapter
import session_adapter
import heartbeat_adapter
import adapter_api


# ============================================================================
# Self-contained fixtures (no shared conftest.py in agent-abide tests)
# ============================================================================

@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    """Create a temporary asdaaas directory structure and monkeypatch all modules to use it.

    Agent-centric structure:
      tmp_path/agents/<AgentName>/asdaaas/{gaze,awareness,health,attention/,doorbells/,profile/,commands}.json
      tmp_path/agents/<AgentName>/asdaaas/adapters/<adapter>/{inbox,outbox}/

    Engine config:
      tmp_path/asdaaas/adapters/<adapter>.json  (adapter registrations)
      tmp_path/asdaaas/running_agents.json      (agent name -> home path map)

    Returns the engine dir (tmp_path/asdaaas).
    """
    hub = tmp_path / "asdaaas"
    agents_home = tmp_path / "agents"

    agents = ["Sr", "Jr", "Trip", "Q", "Cinco"]
    adapters = ["irc", "localmail", "session", "context", "heartbeat", "remind"]

    # Per-agent dirs (agent-centric: ~/agents/<name>/asdaaas/...)
    for agent in agents:
        agent_asdaaas = agents_home / agent / "asdaaas"
        (agent_asdaaas / "doorbells").mkdir(parents=True, exist_ok=True)
        (agent_asdaaas / "attention").mkdir(parents=True, exist_ok=True)
        (agent_asdaaas / "profile").mkdir(parents=True, exist_ok=True)
        # Per-agent adapter queues
        for adapter in adapters:
            (agent_asdaaas / "adapters" / adapter / "inbox").mkdir(parents=True, exist_ok=True)
            (agent_asdaaas / "adapters" / adapter / "outbox").mkdir(parents=True, exist_ok=True)

    # Engine config dirs
    (hub / "adapters").mkdir(parents=True, exist_ok=True)

    # Legacy dirs (still used by some code paths)
    (hub / "inbox").mkdir(parents=True, exist_ok=True)
    (hub / "payloads").mkdir(parents=True, exist_ok=True)
    for agent in agents:
        (hub / "outbox" / agent).mkdir(parents=True, exist_ok=True)
    (hub / "outbox" / "irc").mkdir(parents=True, exist_ok=True)
    (hub / "outbox" / "localmail").mkdir(parents=True, exist_ok=True)

    # Legacy agent dirs (some adapters still reference AGENTS_DIR)
    for agent in agents:
        (hub / "agents" / agent / "doorbells").mkdir(parents=True, exist_ok=True)

    # Monkeypatch asdaaas module
    monkeypatch.setattr(asdaaas, "ASDAAAS_DIR", hub)
    monkeypatch.setattr(asdaaas, "AGENTS_HOME_DIR", agents_home)
    monkeypatch.setattr(asdaaas, "AGENTS_DIR", hub / "agents")
    monkeypatch.setattr(asdaaas, "ADAPTERS_DIR", hub / "adapters")
    monkeypatch.setattr(asdaaas, "HUB_DIR", hub)
    monkeypatch.setattr(asdaaas, "INBOX_DIR", hub / "inbox")
    monkeypatch.setattr(asdaaas, "OUTBOX_DIR", hub / "outbox")
    monkeypatch.setattr(asdaaas, "RUNNING_AGENTS_FILE", hub / "running_agents.json")

    # Monkeypatch adapter_api module
    monkeypatch.setattr(adapter_api, "HUB_DIR", hub)
    monkeypatch.setattr(adapter_api, "AGENTS_DIR", hub / "agents")
    monkeypatch.setattr(adapter_api, "AGENTS_HOME_DIR", agents_home)
    monkeypatch.setattr(adapter_api, "INBOX_DIR", hub / "inbox")
    monkeypatch.setattr(adapter_api, "OUTBOX_DIR", hub / "outbox")
    monkeypatch.setattr(adapter_api, "PER_ADAPTER_DIR", hub / "adapters")
    monkeypatch.setattr(adapter_api, "ADAPTERS_DIR", hub / "adapters")
    monkeypatch.setattr(adapter_api, "PAYLOADS_DIR", hub / "payloads")
    monkeypatch.setattr(adapter_api, "STATUS_QUERY_DIR", hub / "status")
    monkeypatch.setattr(adapter_api, "SESSION_INBOX", hub / "adapters" / "session" / "inbox")

    # Context adapter
    monkeypatch.setattr(context_adapter, "HUB_DIR", hub)
    monkeypatch.setattr(context_adapter, "AGENTS_DIR", hub / "agents")
    monkeypatch.setattr(context_adapter, "AGENTS_HOME_DIR", agents_home)

    # Session adapter
    monkeypatch.setattr(session_adapter, "HUB_DIR", hub)
    monkeypatch.setattr(session_adapter, "AGENTS_DIR", hub / "agents")
    monkeypatch.setattr(session_adapter, "AGENTS_HOME_DIR", agents_home)
    monkeypatch.setattr(session_adapter, "SESSION_INBOX", hub / "adapters" / "session" / "inbox")

    # Heartbeat adapter
    monkeypatch.setattr(heartbeat_adapter, "HUB_DIR", hub)
    monkeypatch.setattr(heartbeat_adapter, "AGENTS_DIR", hub / "agents")
    monkeypatch.setattr(heartbeat_adapter, "AGENTS_HOME_DIR", agents_home)

    return hub


@pytest.fixture
def write_gaze(hub_dir, tmp_path):
    """Helper to write a gaze file for an agent."""
    agents_home = tmp_path / "agents"
    def _write(agent, speech_target="irc", speech_params=None, thoughts_target=None, thoughts_params=None):
        gaze = {}
        if speech_target:
            gaze["speech"] = {"target": speech_target, "params": speech_params or {"channel": "#standup"}}
        else:
            gaze["speech"] = None
        if thoughts_target:
            gaze["thoughts"] = {"target": thoughts_target, "params": thoughts_params or {"channel": f"#{agent.lower()}-thoughts"}}
        else:
            gaze["thoughts"] = None

        agent_d = agents_home / agent / "asdaaas"
        agent_d.mkdir(parents=True, exist_ok=True)
        gaze_file = agent_d / "gaze.json"
        with open(gaze_file, "w") as f:
            json.dump(gaze, f)
        return gaze
    return _write


@pytest.fixture
def write_health(hub_dir, tmp_path):
    """Helper to write a health file for an agent."""
    agents_home = tmp_path / "agents"
    def _write(agent, status="ready", total_tokens=50000):
        health = {
            "agent": agent,
            "status": status,
            "detail": f"session=test-{agent.lower()}",
            "ts": time.time(),
            "pid": os.getpid(),
            "totalTokens": total_tokens,
            "contextWindow": 200000,
            "last_activity": time.time(),
        }
        agent_d = agents_home / agent / "asdaaas"
        agent_d.mkdir(parents=True, exist_ok=True)
        health_file = agent_d / "health.json"
        with open(health_file, "w") as f:
            json.dump(health, f)
        return health
    return _write


# ============================================================================
# Phase 4.4 — CommandWatchdog
# ============================================================================

class TestCommandWatchdog:
    def test_track_and_acknowledge(self, hub_dir):
        wd = asdaaas.CommandWatchdog("Trip")
        wd.track("req-1", "impress", "click_button", timeout=10)
        assert "req-1" in wd.pending
        assert wd.acknowledge("req-1")
        assert "req-1" not in wd.pending

    def test_acknowledge_unknown_returns_false(self, hub_dir):
        wd = asdaaas.CommandWatchdog("Trip")
        assert not wd.acknowledge("nonexistent")

    def test_check_expired_returns_timed_out(self, hub_dir):
        wd = asdaaas.CommandWatchdog("Trip")
        wd.track("req-1", "impress", "click", timeout=0)  # already expired
        expired = wd.check_expired()
        assert len(expired) == 1
        assert expired[0]["request_id"] == "req-1"
        assert expired[0]["adapter"] == "impress"
        assert "req-1" not in wd.pending

    def test_not_expired_stays_pending(self, hub_dir):
        wd = asdaaas.CommandWatchdog("Trip")
        wd.track("req-1", "impress", "click", timeout=9999)
        expired = wd.check_expired()
        assert len(expired) == 0
        assert "req-1" in wd.pending

    def test_deliver_timeout_doorbells_writes_file(self, hub_dir):
        wd = asdaaas.CommandWatchdog("Trip")
        wd.track("req-timeout", "impress", "click", timeout=0)
        expired = wd.deliver_timeout_doorbells("Trip")
        assert len(expired) == 1

        bell_dir = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "doorbells"
        bells = list(bell_dir.glob("*.json"))
        assert len(bells) == 1
        with open(bells[0]) as f:
            bell = json.load(f)
        assert bell["adapter"] == "impress"
        assert bell["error"] is True
        assert bell["priority"] == 1
        assert "TIMEOUT" in bell["text"]
        assert bell["request_id"] == "req-timeout"

    def test_multiple_pending_independent(self, hub_dir):
        wd = asdaaas.CommandWatchdog("Trip")
        wd.track("req-1", "impress", "click", timeout=0)    # expired
        wd.track("req-2", "meet", "share", timeout=9999)     # not expired
        expired = wd.check_expired()
        assert len(expired) == 1
        assert expired[0]["request_id"] == "req-1"
        assert "req-2" in wd.pending

    def test_default_timeout_is_10(self, hub_dir):
        wd = asdaaas.CommandWatchdog("Trip")
        wd.track("req-1", "impress", "click")
        entry = wd.pending["req-1"]
        # Deadline should be ~10s from now
        remaining = entry["deadline"] - time.monotonic()
        assert 9 < remaining < 11


# ============================================================================
# Gaze Matching (inbound filtering)
# ============================================================================

class TestMatchesGaze:
    """Test matches_gaze() -- adapter-agnostic room matching."""

    def test_room_match(self):
        gaze = {"speech": {"target": "irc", "params": {"room": "#standup"}}}
        msg = {"adapter": "irc", "from": "eric", "meta": {"room": "#standup"}}
        assert asdaaas.matches_gaze(msg, gaze) is True

    def test_room_mismatch(self):
        gaze = {"speech": {"target": "irc", "params": {"room": "#standup"}}}
        msg = {"adapter": "irc", "from": "eric", "meta": {"room": "#random"}}
        assert asdaaas.matches_gaze(msg, gaze) is False

    def test_pm_room_match(self):
        gaze = {"speech": {"target": "irc", "params": {"room": "pm:eric"}}}
        msg = {"adapter": "irc", "from": "eric", "meta": {"room": "pm:eric"}}
        assert asdaaas.matches_gaze(msg, gaze) is True

    def test_pm_room_mismatch(self):
        gaze = {"speech": {"target": "irc", "params": {"room": "pm:eric"}}}
        msg = {"adapter": "irc", "from": "Trip", "meta": {"room": "pm:Trip"}}
        assert asdaaas.matches_gaze(msg, gaze) is False

    def test_pm_room_vs_channel(self):
        gaze = {"speech": {"target": "irc", "params": {"room": "pm:eric"}}}
        msg = {"adapter": "irc", "from": "eric", "meta": {"room": "#standup"}}
        assert asdaaas.matches_gaze(msg, gaze) is False

    def test_adapter_mismatch(self):
        gaze = {"speech": {"target": "irc", "params": {"room": "#standup"}}}
        msg = {"adapter": "slack", "from": "eric", "meta": {"room": "#standup"}}
        assert asdaaas.matches_gaze(msg, gaze) is False

    def test_no_room_in_gaze_matches_all_on_adapter(self):
        gaze = {"speech": {"target": "irc", "params": {}}}
        msg = {"adapter": "irc", "from": "eric", "meta": {"room": "#standup"}}
        assert asdaaas.matches_gaze(msg, gaze) is True

    def test_null_speech_matches_nothing(self):
        gaze = {"speech": None}
        msg = {"adapter": "irc", "from": "eric", "meta": {"room": "#standup"}}
        assert asdaaas.matches_gaze(msg, gaze) is False

    def test_no_room_in_msg_doesnt_match_specific_gaze(self):
        gaze = {"speech": {"target": "irc", "params": {"room": "#standup"}}}
        msg = {"adapter": "irc", "from": "eric", "meta": {}}
        assert asdaaas.matches_gaze(msg, gaze) is False

    def test_slack_dm_room(self):
        """Adapter-agnostic: works for Slack DMs too."""
        gaze = {"speech": {"target": "slack", "params": {"room": "dm:eric"}}}
        msg = {"adapter": "slack", "from": "eric", "meta": {"room": "dm:eric"}}
        assert asdaaas.matches_gaze(msg, gaze) is True

    def test_mesh_agent_room(self):
        """Adapter-agnostic: works for mesh agent-to-agent too."""
        gaze = {"speech": {"target": "mesh", "params": {"room": "Jr"}}}
        msg = {"adapter": "mesh", "from": "Jr", "meta": {"room": "Jr"}}
        assert asdaaas.matches_gaze(msg, gaze) is True


class TestGetBackgroundMode:
    """Test get_background_mode() -- per-room background policy."""

    def test_explicit_room_doorbell(self):
        awareness = {"background_channels": {"#standup": "doorbell"}, "background_default": "pending"}
        msg = {"from": "Trip", "meta": {"room": "#standup"}}
        assert asdaaas.get_background_mode(msg, awareness) == "doorbell"

    def test_explicit_room_drop(self):
        awareness = {"background_channels": {"#random": "drop"}, "background_default": "pending"}
        msg = {"from": "Trip", "meta": {"room": "#random"}}
        assert asdaaas.get_background_mode(msg, awareness) == "drop"

    def test_falls_back_to_default(self):
        awareness = {"background_channels": {"#standup": "doorbell"}, "background_default": "pending"}
        msg = {"from": "Trip", "meta": {"room": "#other"}}
        assert asdaaas.get_background_mode(msg, awareness) == "pending"

    def test_default_is_pending_when_unset(self):
        awareness = {}
        msg = {"from": "Trip", "meta": {"room": "#standup"}}
        assert asdaaas.get_background_mode(msg, awareness) == "pending"

    def test_pm_room_as_key(self):
        awareness = {"background_channels": {"pm:eric": "doorbell"}, "background_default": "drop"}
        msg = {"from": "eric", "meta": {"room": "pm:eric"}}
        assert asdaaas.get_background_mode(msg, awareness) == "doorbell"

    def test_no_room_falls_back_to_default(self):
        awareness = {"background_channels": {}, "background_default": "drop"}
        msg = {"from": "Trip", "meta": {}}
        assert asdaaas.get_background_mode(msg, awareness) == "drop"


class TestFormatBackgroundDoorbell:
    """Test format_background_doorbell() -- adapter-agnostic summary."""

    def test_message_with_room(self):
        msg = {"from": "Trip", "adapter": "irc", "meta": {"room": "#standup"}, "text": "hello world"}
        result = asdaaas.format_background_doorbell(msg)
        assert "[background]" in result
        assert "Trip" in result
        assert "#standup" in result
        assert "hello world" in result

    def test_message_without_room(self):
        msg = {"from": "eric", "adapter": "irc", "meta": {}, "text": "hey"}
        result = asdaaas.format_background_doorbell(msg)
        assert "[background]" in result
        assert "eric" in result
        assert "irc" in result

    def test_long_text_truncated(self):
        msg = {"from": "Trip", "adapter": "irc", "meta": {"room": "#standup"}, "text": "x" * 200}
        result = asdaaas.format_background_doorbell(msg)
        assert "..." in result
        assert len(result) < 300

    def test_long_text_payload_stored(self, tmp_path, monkeypatch):
        """When text > 120 chars and agent_name provided, full message saved to payload file."""
        monkeypatch.setattr(asdaaas, "AGENTS_HOME_DIR", tmp_path)
        msg = {"from": "Trip", "adapter": "irc", "id": "test123",
               "meta": {"room": "#standup"}, "text": "A" * 200}
        result = asdaaas.format_background_doorbell(msg, agent_name="Sr")
        assert "Full message: cat" in result
        assert "test123.json" in result
        payload_path = tmp_path / "Sr" / "asdaaas" / "adapters" / "irc" / "payloads" / "test123.json"
        assert payload_path.exists()
        stored = json.loads(payload_path.read_text())
        assert stored["text"] == "A" * 200
        assert stored["from"] == "Trip"

    def test_short_text_no_payload(self, tmp_path, monkeypatch):
        """Messages under 120 chars should not create payload files."""
        monkeypatch.setattr(asdaaas, "AGENTS_HOME_DIR", tmp_path)
        msg = {"from": "Trip", "adapter": "irc", "meta": {}, "text": "short msg"}
        result = asdaaas.format_background_doorbell(msg, agent_name="Sr")
        assert "Full message" not in result
        assert "short msg" in result

    def test_payload_no_agent_name(self):
        """Without agent_name, long messages truncate but don't store payload."""
        msg = {"from": "Trip", "adapter": "irc", "meta": {}, "text": "B" * 200}
        result = asdaaas.format_background_doorbell(msg)
        assert "..." in result
        assert "Full message" not in result

    def test_payload_includes_size_info(self, tmp_path, monkeypatch):
        """Payload hint includes KB size and approximate token count."""
        monkeypatch.setattr(asdaaas, "AGENTS_HOME_DIR", tmp_path)
        text = "C" * 400
        msg = {"from": "eric", "adapter": "irc", "id": "size_test",
               "meta": {"room": "#standup"}, "text": text}
        result = asdaaas.format_background_doorbell(msg, agent_name="Q")
        assert "KB" in result
        assert "tokens" in result


class TestPendingQueue:
    """Test PendingQueue -- adapter-agnostic room queuing."""

    def test_add_and_drain(self):
        pq = asdaaas.PendingQueue()
        msg = {"from": "Trip", "meta": {"room": "#standup"}, "text": "hello"}
        pq.add(msg)
        assert pq.total == 1

        gaze = {"speech": {"target": "irc", "params": {"room": "#standup"}}}
        drained = pq.drain_for_gaze(gaze)
        assert len(drained) == 1
        assert drained[0]["text"] == "hello"
        assert pq.total == 0

    def test_drain_wrong_room_returns_empty(self):
        pq = asdaaas.PendingQueue()
        msg = {"from": "Trip", "meta": {"room": "#standup"}, "text": "hello"}
        pq.add(msg)

        gaze = {"speech": {"target": "irc", "params": {"room": "#random"}}}
        drained = pq.drain_for_gaze(gaze)
        assert len(drained) == 0
        assert pq.total == 1

    def test_pm_room_pending(self):
        pq = asdaaas.PendingQueue()
        msg = {"from": "eric", "meta": {"room": "pm:eric"}, "text": "hey"}
        pq.add(msg)

        gaze = {"speech": {"target": "irc", "params": {"room": "pm:eric"}}}
        drained = pq.drain_for_gaze(gaze)
        assert len(drained) == 1

    def test_multiple_messages_same_room(self):
        pq = asdaaas.PendingQueue()
        pq.add({"from": "Trip", "meta": {"room": "#standup"}, "text": "msg1"})
        pq.add({"from": "Q", "meta": {"room": "#standup"}, "text": "msg2"})
        assert pq.total == 2

        gaze = {"speech": {"target": "irc", "params": {"room": "#standup"}}}
        drained = pq.drain_for_gaze(gaze)
        assert len(drained) == 2

    def test_multiple_rooms_independent(self):
        pq = asdaaas.PendingQueue()
        pq.add({"from": "Trip", "meta": {"room": "#standup"}, "text": "msg1"})
        pq.add({"from": "Q", "meta": {"room": "#random"}, "text": "msg2"})
        assert pq.total == 2

        gaze = {"speech": {"target": "irc", "params": {"room": "#standup"}}}
        drained = pq.drain_for_gaze(gaze)
        assert len(drained) == 1
        assert pq.total == 1  # #random still queued


# ============================================================================
# Legacy Phase 5 — Callback Override (kept for gaze construction tests)
# ============================================================================

class TestGazeConstruction:
    """Test gaze file reading and construction patterns."""

    def test_default_gaze_used_for_thoughts(self, hub_dir, write_gaze):
        """Thoughts go to the agent's configured gaze."""
        write_gaze("Trip", speech_target="irc", thoughts_target="irc",
                    thoughts_params={"channel": "#trip-thoughts"})
        default_gaze = asdaaas.read_gaze("Trip")
        assert default_gaze["thoughts"]["params"]["channel"] == "#trip-thoughts"


# ============================================================================
# Phase 6.1 — Context Adapter ThresholdTracker
# ============================================================================

class TestThresholdTracker:
    def test_no_fire_below_first_threshold(self):
        tracker = context_adapter.ThresholdTracker()
        to_fire = tracker.check("Trip", 80000, 200000)  # 40%
        assert len(to_fire) == 0

    def test_fire_at_80_percent(self):
        tracker = context_adapter.ThresholdTracker()
        to_fire = tracker.check("Trip", 160000, 200000)  # 80%
        assert len(to_fire) == 1
        assert to_fire[0]["pct"] == 80
        assert to_fire[0]["level"] == "reminder"

    def test_no_fire_below_80(self):
        tracker = context_adapter.ThresholdTracker()
        to_fire = tracker.check("Trip", 140000, 200000)  # 70%
        assert len(to_fire) == 0

    def test_no_double_fire(self):
        tracker = context_adapter.ThresholdTracker()
        tracker.check("Trip", 160000, 200000)  # 80% -- fires
        to_fire = tracker.check("Trip", 161000, 200000)  # still ~80% -- should NOT fire again
        assert len(to_fire) == 0

    def test_reset_after_compaction(self):
        tracker = context_adapter.ThresholdTracker()
        tracker.check("Trip", 170000, 200000)  # 85% -- fires 80
        # Compaction drops to 20%
        to_fire = tracker.check("Trip", 40000, 200000)
        # Should have reset -- no new thresholds crossed yet
        assert len(to_fire) == 0
        # Now climb back up
        to_fire = tracker.check("Trip", 165000, 200000)  # 82.5%
        assert len(to_fire) == 1
        assert to_fire[0]["pct"] == 80

    def test_independent_agents(self):
        tracker = context_adapter.ThresholdTracker()
        tracker.check("Trip", 160000, 200000)  # Trip at 80%
        to_fire = tracker.check("Q", 160000, 200000)  # Q at 80% -- should fire independently
        assert len(to_fire) == 1

    def test_zero_context_window(self):
        tracker = context_adapter.ThresholdTracker()
        to_fire = tracker.check("Trip", 100000, 0)
        assert len(to_fire) == 0

    def test_all_thresholds_fire(self):
        tracker = context_adapter.ThresholdTracker()
        to_fire = tracker.check("Trip", 190000, 200000)  # 95%
        fired_pcts = {t["pct"] for t in to_fire}
        assert fired_pcts == {80}

    def test_single_threshold_priority(self):
        tracker = context_adapter.ThresholdTracker()
        to_fire = tracker.check("Trip", 190000, 200000)  # 95%
        assert len(to_fire) == 1
        assert to_fire[0]["priority"] == 3


class TestContextDoorbellWriting:
    def test_ring_context_doorbell(self, hub_dir):
        context_adapter.AGENTS_HOME_DIR = hub_dir.parent / "agents"
        threshold = {"pct": 65, "priority": 3, "level": "advisory", "advice": "wrap up tasks"}
        context_adapter.ring_context_doorbell("Trip", threshold, 130000, 200000)

        bell_dir = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "doorbells"
        bells = list(bell_dir.glob("*.json"))
        assert len(bells) == 1
        with open(bells[0]) as f:
            bell = json.load(f)
        assert bell["adapter"] == "context"
        assert bell["priority"] == 3
        assert bell["threshold"] == 65
        assert "65%" in bell["text"]
        assert bell["totalTokens"] == 130000


class TestContextHealthReading:
    def test_read_agent_health(self, hub_dir, write_health):
        context_adapter.AGENTS_HOME_DIR = hub_dir.parent / "agents"
        write_health("Trip", status="active", total_tokens=130000)
        result = context_adapter.read_agent_health("Trip")
        assert result is not None
        total, window = result
        assert total == 130000
        assert window == 200000

    def test_read_missing_agent(self, hub_dir):
        context_adapter.AGENTS_HOME_DIR = hub_dir.parent / "agents"
        result = context_adapter.read_agent_health("Nonexistent")
        assert result is None


# ============================================================================
# Phase 6.2 — Session Adapter
# ============================================================================

class TestSessionHandleStatus:
    def test_handle_status_returns_health(self, hub_dir, write_health):
        session_adapter.AGENTS_HOME_DIR = hub_dir.parent / "agents"
        write_health("Trip", status="active", total_tokens=130000)
        result = session_adapter.handle_status("Trip")
        assert "error" not in result
        assert result["totalTokens"] == 130000
        assert result["usage_pct"] == 65.0
        assert result["status"] == "active"

    def test_handle_status_missing_agent(self, hub_dir):
        session_adapter.AGENTS_HOME_DIR = hub_dir.parent / "agents"
        result = session_adapter.handle_status("Nonexistent")
        assert "error" in result
        assert result["error"] == "no_health_file"


class TestSessionDoorbellWriting:
    def test_compact_success_doorbell(self, hub_dir):
        session_adapter.AGENTS_HOME_DIR = hub_dir.parent / "agents"
        result = {"before": 180000, "after": 45000}
        session_adapter.ring_session_doorbell("Trip", "compact", "req-1", result)

        bell_dir = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "doorbells"
        bells = list(bell_dir.glob("*.json"))
        assert len(bells) == 1
        with open(bells[0]) as f:
            bell = json.load(f)
        assert bell["adapter"] == "session"
        assert bell["command"] == "compact"
        assert bell["request_id"] == "req-1"
        assert "75%" in bell["text"]

    def test_compact_error_doorbell(self, hub_dir):
        session_adapter.AGENTS_HOME_DIR = hub_dir.parent / "agents"
        result = {"error": "timeout", "detail": "Compact did not complete"}
        session_adapter.ring_session_doorbell("Trip", "compact", "req-1", result)

        bell_dir = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "doorbells"
        bells = list(bell_dir.glob("*.json"))
        assert len(bells) == 1
        with open(bells[0]) as f:
            bell = json.load(f)
        assert bell["priority"] == 1  # high priority for errors
        assert "FAILED" in bell["text"]

    def test_status_doorbell(self, hub_dir):
        session_adapter.AGENTS_HOME_DIR = hub_dir.parent / "agents"
        result = {"status": "active", "usage_pct": 65.0, "totalTokens": 130000, "contextWindow": 200000}
        session_adapter.ring_session_doorbell("Trip", "status", "req-2", result)

        bell_dir = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "doorbells"
        bells = list(bell_dir.glob("*.json"))
        assert len(bells) == 1
        with open(bells[0]) as f:
            bell = json.load(f)
        assert "65" in bell["text"]


class TestSessionInboxPolling:
    def test_poll_session_inbox(self, hub_dir):
        session_adapter.AGENTS_HOME_DIR = hub_dir.parent / "agents"
        inbox = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "adapters" / "session" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)

        cmd = {"command": "compact", "from": "Trip", "request_id": "req-1"}
        with open(inbox / "cmd_001.json", "w") as f:
            json.dump(cmd, f)

        commands = session_adapter.poll_session_inbox("Trip")
        assert len(commands) == 1
        assert commands[0]["command"] == "compact"
        # File should be deleted
        assert not list(inbox.glob("*.json"))

    def test_poll_empty_inbox(self, hub_dir):
        session_adapter.AGENTS_HOME_DIR = hub_dir.parent / "agents"
        commands = session_adapter.poll_session_inbox("Nonexistent")
        assert commands == []


# ============================================================================
# Phase 6.3 — Heartbeat Adapter IdleTracker
# ============================================================================

class TestIdleTracker:
    def test_no_nudge_when_active(self):
        tracker = heartbeat_adapter.IdleTracker(idle_threshold=900, nudge_interval=600)
        health = {"last_activity": time.time()}
        should_nudge, idle = tracker.check("Trip", health)
        assert not should_nudge

    def test_nudge_when_idle(self):
        tracker = heartbeat_adapter.IdleTracker(idle_threshold=60, nudge_interval=60)
        past = time.time() - 120
        health = {"last_activity": past}
        should_nudge, idle = tracker.check("Trip", health)
        assert should_nudge
        assert idle >= 120

    def test_no_double_nudge_within_interval(self):
        tracker = heartbeat_adapter.IdleTracker(idle_threshold=60, nudge_interval=600)
        past = time.time() - 120
        health = {"last_activity": past}

        should_nudge1, _ = tracker.check("Trip", health)
        assert should_nudge1

        should_nudge2, _ = tracker.check("Trip", health)
        assert not should_nudge2  # too soon after first nudge

    def test_no_nudge_when_agent_becomes_active(self):
        tracker = heartbeat_adapter.IdleTracker(idle_threshold=60, nudge_interval=60)

        # Agent is idle — gets nudged
        past = time.time() - 120
        health_idle = {"last_activity": past}
        should_nudge, _ = tracker.check("Trip", health_idle)
        assert should_nudge

        # Agent becomes active — no nudge, but last_nudge is preserved
        # (so nudge_interval is enforced even after agent responds)
        health_active = {"last_activity": time.time()}
        should_nudge, _ = tracker.check("Trip", health_active)
        assert not should_nudge
        assert "Trip" in tracker.last_nudge  # preserved for interval enforcement

    def test_nudge_interval_survives_activity_reset(self):
        """The bug Q reported: agent responds to nudge (resetting idle timer),
        then goes idle again. Without interval enforcement, the agent gets
        nudged every idle_threshold seconds instead of every nudge_interval."""
        tracker = heartbeat_adapter.IdleTracker(idle_threshold=60, nudge_interval=3600)

        # 1. Agent idle 120s — first nudge fires
        past = time.time() - 120
        health = {"last_activity": past}
        should_nudge, _ = tracker.check("Q", health)
        assert should_nudge

        # 2. Agent responds to nudge — goes active, then idle again for 120s
        #    (simulating: response updated last_activity, then 120s passed)
        past2 = time.time() - 120
        health2 = {"last_activity": past2}
        should_nudge, _ = tracker.check("Q", health2)
        # Should NOT nudge — only 120s since last nudge, interval is 3600
        assert not should_nudge

    def test_independent_agents(self):
        tracker = heartbeat_adapter.IdleTracker(idle_threshold=60, nudge_interval=60)
        past = time.time() - 120
        health = {"last_activity": past}

        should_trip, _ = tracker.check("Trip", health)
        should_q, _ = tracker.check("Q", health)
        assert should_trip
        assert should_q

    def test_missing_last_activity(self):
        tracker = heartbeat_adapter.IdleTracker(idle_threshold=60, nudge_interval=60)
        health = {}
        should_nudge, _ = tracker.check("Trip", health)
        assert not should_nudge


class TestFormatIdleTime:
    def test_seconds(self):
        assert "30 seconds" in heartbeat_adapter.format_idle_time(30)

    def test_minutes(self):
        assert "5 minutes" in heartbeat_adapter.format_idle_time(300)

    def test_one_minute(self):
        assert "1 minute" in heartbeat_adapter.format_idle_time(60)

    def test_hours(self):
        result = heartbeat_adapter.format_idle_time(7200)
        assert "2 hours" in result

    def test_hours_and_minutes(self):
        result = heartbeat_adapter.format_idle_time(5400)  # 1.5 hours
        assert "1 hour" in result
        assert "30 min" in result


class TestHeartbeatDoorbellWriting:
    def test_ring_heartbeat_doorbell(self, hub_dir):
        heartbeat_adapter.AGENTS_HOME_DIR = hub_dir.parent / "agents"
        heartbeat_adapter.ring_heartbeat_doorbell("Trip", 900)

        bell_dir = hub_dir.parent / "agents" / "Trip" / "asdaaas" / "doorbells"
        bells = list(bell_dir.glob("*.json"))
        assert len(bells) == 1
        with open(bells[0]) as f:
            bell = json.load(f)
        assert bell["adapter"] == "heartbeat"
        assert bell["priority"] == 5
        assert bell["idle_seconds"] == 900
        assert "15 minutes" in bell["text"]


# ============================================================================
# Phase 7.2 — Adapter Registration Reader
# ============================================================================

class TestReadAdapterRegistrations:
    def test_reads_registration_files(self, hub_dir):
        # Write a registration file
        reg = {
            "name": "test_adapter",
            "pid": os.getpid(),
            "started": "2026-03-26T12:00:00",
            "capabilities": ["send", "receive"],
            "config": {},
            "heartbeat": "2026-03-26T12:00:00",
        }
        reg_file = hub_dir / "adapters" / "test_adapter.json"
        with open(reg_file, "w") as f:
            json.dump(reg, f)

        registrations = asdaaas.read_adapter_registrations()
        assert "test_adapter" in registrations
        assert registrations["test_adapter"]["alive"] is True  # our own PID

    def test_dead_pid_marked_not_alive(self, hub_dir):
        reg = {
            "name": "dead_adapter",
            "pid": 99999999,  # nonexistent PID
            "started": "2026-03-26T12:00:00",
            "capabilities": ["send"],
            "config": {},
        }
        reg_file = hub_dir / "adapters" / "dead_adapter.json"
        with open(reg_file, "w") as f:
            json.dump(reg, f)

        registrations = asdaaas.read_adapter_registrations()
        assert "dead_adapter" in registrations
        assert registrations["dead_adapter"]["alive"] is False

    def test_skips_directories(self, hub_dir):
        # adapters/irc/ is a directory, not a registration file
        registrations = asdaaas.read_adapter_registrations()
        # Should not crash, should skip directories
        assert isinstance(registrations, dict)

    def test_handles_corrupt_json(self, hub_dir):
        corrupt_file = hub_dir / "adapters" / "corrupt.json"
        with open(corrupt_file, "w") as f:
            f.write("not valid json{{{")

        registrations = asdaaas.read_adapter_registrations()
        assert "corrupt" not in registrations

    def test_empty_directory(self, hub_dir):
        # Remove all files from adapters dir (keep subdirs)
        for f in (hub_dir / "adapters").iterdir():
            if f.is_file():
                f.unlink()
        registrations = asdaaas.read_adapter_registrations()
        assert registrations == {}


# ============================================================================
# Phase 7.1 — Adapter API registration (already existed, verify it works)
# ============================================================================

class TestAdapterRegistration:
    def test_register_and_list(self, hub_dir):
        adapter_api.register_adapter(
            name="test_irc",
            capabilities=["send", "receive", "broadcast"],
            config={"channel": "#standup"},
        )

        adapters = adapter_api.list_adapters(max_heartbeat_age=0)
        names = [a["name"] for a in adapters]
        assert "test_irc" in names

    def test_heartbeat_update(self, hub_dir):
        adapter_api.register_adapter(name="test_hb", capabilities=["send"])
        time.sleep(0.1)
        adapter_api.update_heartbeat("test_hb")

        reg = adapter_api.get_adapter("test_hb")
        assert reg is not None
        assert "heartbeat" in reg

    def test_deregister(self, hub_dir):
        adapter_api.register_adapter(name="test_dereg", capabilities=["send"])
        adapter_api.deregister_adapter("test_dereg")
        reg = adapter_api.get_adapter("test_dereg")
        assert reg is None


# ============================================================================
# Per-Agent Heartbeat Preferences (awareness file)
# ============================================================================

class TestHeartbeatPrefs:
    def test_get_heartbeat_prefs_present(self):
        awareness = {"heartbeat": {"idle_threshold": 1800, "nudge_interval": 3600}}
        idle, nudge = heartbeat_adapter.get_heartbeat_prefs(awareness)
        assert idle == 1800
        assert nudge == 3600

    def test_get_heartbeat_prefs_partial(self):
        awareness = {"heartbeat": {"idle_threshold": 300}}
        idle, nudge = heartbeat_adapter.get_heartbeat_prefs(awareness)
        assert idle == 300
        assert nudge is None

    def test_get_heartbeat_prefs_missing(self):
        awareness = {"direct_attach": ["irc"]}
        idle, nudge = heartbeat_adapter.get_heartbeat_prefs(awareness)
        assert idle is None
        assert nudge is None

    def test_get_heartbeat_prefs_empty_awareness(self):
        idle, nudge = heartbeat_adapter.get_heartbeat_prefs({})
        assert idle is None
        assert nudge is None

    def test_get_heartbeat_prefs_invalid_type(self):
        awareness = {"heartbeat": "fast"}
        idle, nudge = heartbeat_adapter.get_heartbeat_prefs(awareness)
        assert idle is None
        assert nudge is None

    def test_get_heartbeat_prefs_negative_values(self):
        awareness = {"heartbeat": {"idle_threshold": -100, "nudge_interval": 0}}
        idle, nudge = heartbeat_adapter.get_heartbeat_prefs(awareness)
        assert idle is None
        assert nudge is None

    def test_idle_tracker_per_agent_override(self):
        tracker = heartbeat_adapter.IdleTracker(idle_threshold=900, nudge_interval=600)
        past = time.time() - 120
        health = {"last_activity": past}
        # Default threshold is 900s, agent is only 120s idle -- no nudge
        should_nudge, _ = tracker.check("Trip", health)
        assert not should_nudge
        # Per-agent override: threshold 60s -- should nudge now
        should_nudge, _ = tracker.check("Trip", health, idle_threshold=60)
        assert should_nudge

    def test_idle_tracker_per_agent_nudge_interval(self):
        tracker = heartbeat_adapter.IdleTracker(idle_threshold=60, nudge_interval=99999)
        past = time.time() - 120
        health = {"last_activity": past}
        # First nudge fires
        should_nudge, _ = tracker.check("Trip", health, nudge_interval=99999)
        assert should_nudge
        # Default interval is huge, so second nudge should NOT fire
        should_nudge, _ = tracker.check("Trip", health, nudge_interval=99999)
        assert not should_nudge

    def test_read_agent_awareness(self, hub_dir):
        heartbeat_adapter.AGENTS_HOME_DIR = hub_dir.parent / "agents"
        awareness = {"heartbeat": {"idle_threshold": 1800}, "direct_attach": ["irc"]}
        with open(hub_dir.parent / "agents" / "Trip" / "asdaaas" / "awareness.json", "w") as f:
            json.dump(awareness, f)
        result = heartbeat_adapter.read_agent_awareness("Trip")
        assert result["heartbeat"]["idle_threshold"] == 1800

    def test_read_agent_awareness_missing(self, hub_dir):
        heartbeat_adapter.AGENTS_HOME_DIR = hub_dir.parent / "agents"
        result = heartbeat_adapter.read_agent_awareness("Nobody")
        assert result == {}


# ============================================================================
# Per-Agent Context Threshold Preferences (awareness file)
# ============================================================================

class TestContextThresholdPrefs:
    def test_get_context_thresholds_present(self):
        awareness = {"context_thresholds": [30, 50, 75, 90]}
        thresholds = context_adapter.get_context_thresholds(awareness)
        assert thresholds is not None
        assert len(thresholds) == 4
        pcts = [t["pct"] for t in thresholds]
        assert pcts == [30, 50, 75, 90]  # sorted ascending

    def test_get_context_thresholds_missing(self):
        awareness = {"direct_attach": ["irc"]}
        thresholds = context_adapter.get_context_thresholds(awareness)
        assert thresholds is None

    def test_get_context_thresholds_empty_list(self):
        awareness = {"context_thresholds": []}
        thresholds = context_adapter.get_context_thresholds(awareness)
        assert thresholds is None

    def test_get_context_thresholds_invalid_type(self):
        awareness = {"context_thresholds": "high"}
        thresholds = context_adapter.get_context_thresholds(awareness)
        assert thresholds is None

    def test_get_context_thresholds_filters_bad_values(self):
        awareness = {"context_thresholds": [50, -10, 0, 101, 80]}
        thresholds = context_adapter.get_context_thresholds(awareness)
        assert len(thresholds) == 2
        pcts = [t["pct"] for t in thresholds]
        assert pcts == [50, 80]

    def test_get_context_thresholds_level_assignment(self):
        awareness = {"context_thresholds": [30, 70, 85, 90]}
        thresholds = context_adapter.get_context_thresholds(awareness)
        levels = {t["pct"]: t["level"] for t in thresholds}
        assert levels[30] == "info"
        assert levels[70] == "info"
        assert levels[85] == "reminder"
        assert levels[90] == "reminder"

    def test_level_for_pct(self):
        _, level, _ = context_adapter._level_for_pct(90)
        assert level == "reminder"
        _, level, _ = context_adapter._level_for_pct(80)
        assert level == "reminder"
        _, level, _ = context_adapter._level_for_pct(65)
        assert level == "info"
        _, level, _ = context_adapter._level_for_pct(40)
        assert level == "info"

    def test_threshold_tracker_per_agent_override(self):
        tracker = context_adapter.ThresholdTracker()
        # Default thresholds: 80 only
        # At 50%, nothing should fire with defaults
        to_fire = tracker.check("Trip", 100000, 200000)
        assert len(to_fire) == 0

        # Now use per-agent thresholds for Q: [30, 50]
        custom = [
            {"pct": 30, "priority": 5, "level": "info", "advice": "checkpoint"},
            {"pct": 50, "priority": 3, "level": "reminder", "advice": "halfway"},
        ]
        to_fire = tracker.check("Q", 100000, 200000, thresholds=custom)  # 50%
        pcts = {t["pct"] for t in to_fire}
        assert pcts == {30, 50}

    def test_threshold_tracker_per_agent_does_not_affect_other(self):
        tracker = context_adapter.ThresholdTracker()
        custom = [{"pct": 10, "priority": 5, "level": "info", "advice": "early"}]
        # Q gets custom thresholds
        to_fire_q = tracker.check("Q", 30000, 200000, thresholds=custom)  # 15%
        assert len(to_fire_q) == 1
        assert to_fire_q[0]["pct"] == 10
        # Trip uses defaults at same 15% -- nothing should fire
        to_fire_trip = tracker.check("Trip", 30000, 200000)
        assert len(to_fire_trip) == 0

    def test_read_agent_awareness_context(self, hub_dir):
        context_adapter.AGENTS_HOME_DIR = hub_dir.parent / "agents"
        awareness = {"context_thresholds": [40, 70, 85]}
        with open(hub_dir.parent / "agents" / "Q" / "asdaaas" / "awareness.json", "w") as f:
            json.dump(awareness, f)
        result = context_adapter.read_agent_awareness("Q")
        thresholds = context_adapter.get_context_thresholds(result)
        assert len(thresholds) == 3
