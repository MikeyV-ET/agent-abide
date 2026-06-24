"""End-to-end tests: multi-agent, multi-user message routing.

Ported from mikeyv-infra/tests/test_e2e.py to agent-abide.

Tests the full message flow through the asdaaas filesystem interface:
  MockUser -> adapter inbox -> asdaaas routing -> agent prompt -> agent response -> outbox

No network, no real grok binary. Everything is filesystem-based.
Exercises: gaze matching, background channels, doorbell delivery,
inter-agent messaging, room routing, PM routing.

Usage:
    pytest tests/test_e2e_multiagent.py -v
"""

import json
import os
import sys
import time
import pytest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))

import asdaaas
import adapter_api


# ============================================================================
# Test Environment
# ============================================================================

class E2EEnvironment:
    """Sets up a multi-agent test environment on a temp filesystem.

    Creates agent directories, gaze files, awareness configs.
    Provides MockUser helpers to inject messages and assert on delivery.
    """

    def __init__(self, tmp_path, agent_names):
        self.tmp_path = tmp_path
        self.agent_names = agent_names

        # Save originals for teardown
        self._orig_asdaaas_home = asdaaas.AGENTS_HOME_DIR
        self._orig_adapter_home = adapter_api.AGENTS_HOME_DIR

        # Point both modules at our temp dir
        asdaaas.AGENTS_HOME_DIR = tmp_path / "agents"
        adapter_api.AGENTS_HOME_DIR = tmp_path / "agents"

        # Create agent directories
        for name in agent_names:
            agent_dir = tmp_path / "agents" / name / "asdaaas"
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "doorbells").mkdir(exist_ok=True)
            (agent_dir / "attention").mkdir(exist_ok=True)
            (agent_dir / "adapters" / "irc" / "inbox").mkdir(parents=True, exist_ok=True)
            (agent_dir / "adapters" / "irc" / "outbox").mkdir(parents=True, exist_ok=True)
            (agent_dir / "adapters" / "localmail" / "inbox").mkdir(parents=True, exist_ok=True)
            (agent_dir / "adapters" / "localmail" / "outbox").mkdir(parents=True, exist_ok=True)

            # Default gaze: pm:eric
            asdaaas.write_gaze(name, {
                "speech": {"target": "irc", "params": {"room": "pm:eric"}},
                "thoughts": None
            })

            # Default awareness
            asdaaas.write_awareness(name, {
                "direct_attach": ["irc", "localmail"],
                "background_channels": {"#standup": "doorbell", "pm:eric": "doorbell"},
                "background_default": "pending",
                "default_doorbell": True,
                "doorbell_ttl": {"heartbeat": 1, "irc": 3, "default": 3},
            })

    def teardown(self):
        asdaaas.AGENTS_HOME_DIR = self._orig_asdaaas_home
        adapter_api.AGENTS_HOME_DIR = self._orig_adapter_home

    def set_gaze(self, agent_name, adapter, room=None, pm=None):
        """Set agent gaze using the command builder."""
        cmd = {"action": "gaze", "adapter": adapter}
        if room:
            cmd["room"] = room
        if pm:
            cmd["pm"] = pm
        gaze = asdaaas._build_gaze(cmd)
        asdaaas.write_gaze(agent_name, gaze)

    def set_awareness(self, agent_name, **kwargs):
        """Apply awareness command."""
        current = asdaaas.read_awareness(agent_name)
        cmd = {"action": "awareness"}
        cmd.update(kwargs)
        updated, desc = asdaaas._apply_awareness_command(cmd, current)
        if updated:
            asdaaas.write_awareness(agent_name, updated)
        return desc

    def inject_irc_message(self, to_agent, sender, text, room="#standup"):
        """Simulate an IRC message arriving in an agent's adapter inbox."""
        adapter_api.write_to_adapter_inbox(
            adapter_name="irc",
            to=to_agent,
            text=f"[IRC {room} from {sender}]\n{text}",
            sender=sender,
            meta={"room": room, "channel": room, "senders": [sender]},
        )

    def inject_localmail(self, from_agent, to_agent, text):
        """Simulate a localmail message arriving."""
        adapter_api.write_to_adapter_inbox(
            adapter_name="localmail",
            to=to_agent,
            text=text,
            sender=from_agent,
            meta={"from": from_agent},
        )

    def poll_messages(self, agent_name):
        """Poll all adapter inboxes for an agent. Returns list of message dicts."""
        awareness = asdaaas.read_awareness(agent_name)
        return asdaaas.poll_adapter_inboxes(agent_name, awareness)

    def get_doorbells(self, agent_name):
        """Read all pending doorbells for an agent."""
        bell_dir = self.tmp_path / "agents" / agent_name / "asdaaas" / "doorbells"
        bells = []
        for f in sorted(bell_dir.glob("*.json")):
            try:
                with open(f) as fh:
                    bells.append(json.load(fh))
            except (json.JSONDecodeError, OSError):
                pass
        return bells

    def write_doorbell(self, agent_name, text, adapter="irc", priority=5, meta=None):
        """Write a doorbell directly (for testing doorbell delivery).

        Note: asdaaas.write_doorbell does not exist in agent-abide.
        Direct file write instead.
        """
        bell_dir = self.tmp_path / "agents" / agent_name / "asdaaas" / "doorbells"
        bell_dir.mkdir(parents=True, exist_ok=True)
        bell = {
            "text": text,
            "adapter": adapter,
            "priority": priority,
            "meta": meta or {},
            "ts": time.time(),
        }
        bell_path = bell_dir / f"bell_{time.time_ns()}.json"
        bell_path.write_text(json.dumps(bell))


class MockUser:
    """Simulates a human user sending messages through adapters."""

    def __init__(self, name, env):
        self.name = name
        self.env = env

    def say_in_channel(self, channel, text, to_agents=None):
        """Send a message to a channel. Delivers to specified agents or all."""
        targets = to_agents or self.env.agent_names
        for agent in targets:
            self.env.inject_irc_message(agent, self.name, text, room=channel)

    def pm(self, agent_name, text):
        """Send a PM to a specific agent."""
        self.env.inject_irc_message(agent_name, self.name, text, room=f"pm:{self.name}")


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def env(tmp_path):
    """Two-agent environment: AgentA and AgentB."""
    e = E2EEnvironment(tmp_path, ["AgentA", "AgentB"])
    yield e
    e.teardown()


@pytest.fixture
def three_agent_env(tmp_path):
    """Three-agent environment: AgentA, AgentB, AgentC."""
    e = E2EEnvironment(tmp_path, ["AgentA", "AgentB", "AgentC"])
    yield e
    e.teardown()


@pytest.fixture
def eric(env):
    return MockUser("eric", env)


@pytest.fixture
def gwen(env):
    return MockUser("gwen", env)


# ============================================================================
# Gaze Routing Tests
# ============================================================================

class TestGazeRouting:
    """Test that messages match gaze correctly."""

    def test_foreground_when_gaze_matches_room(self, env, eric):
        """Agent gazing at #standup receives #standup messages as foreground."""
        env.set_gaze("AgentA", "irc", room="#standup")
        eric.say_in_channel("#standup", "hello everyone", to_agents=["AgentA"])

        msgs = env.poll_messages("AgentA")
        assert len(msgs) == 1

        gaze = asdaaas.read_gaze("AgentA")
        assert asdaaas.matches_gaze(msgs[0], gaze) is True

    def test_background_when_gaze_different_room(self, env, eric):
        """Agent gazing at pm:eric does NOT get #standup as foreground."""
        env.set_gaze("AgentA", "irc", pm="eric")
        eric.say_in_channel("#standup", "hello", to_agents=["AgentA"])

        msgs = env.poll_messages("AgentA")
        assert len(msgs) == 1

        gaze = asdaaas.read_gaze("AgentA")
        assert asdaaas.matches_gaze(msgs[0], gaze) is False

    def test_pm_foreground(self, env, eric):
        """Agent gazing at pm:eric receives eric's PM as foreground."""
        env.set_gaze("AgentA", "irc", pm="eric")
        eric.pm("AgentA", "hey sr")

        msgs = env.poll_messages("AgentA")
        assert len(msgs) == 1

        gaze = asdaaas.read_gaze("AgentA")
        assert asdaaas.matches_gaze(msgs[0], gaze) is True

    def test_meetingroom_foreground(self, env, eric):
        """Agent gazing at #meetingroom1 receives messages from that room."""
        env.set_gaze("AgentA", "irc", room="#meetingroom1")
        eric.say_in_channel("#meetingroom1", "test from meetingroom", to_agents=["AgentA"])

        msgs = env.poll_messages("AgentA")
        assert len(msgs) == 1

        gaze = asdaaas.read_gaze("AgentA")
        assert asdaaas.matches_gaze(msgs[0], gaze) is True

    def test_meetingroom_not_foreground_when_gazing_standup(self, env, eric):
        """#meetingroom1 message is NOT foreground when gazing at #standup."""
        env.set_gaze("AgentA", "irc", room="#standup")
        eric.say_in_channel("#meetingroom1", "test", to_agents=["AgentA"])

        msgs = env.poll_messages("AgentA")
        assert len(msgs) == 1

        gaze = asdaaas.read_gaze("AgentA")
        assert asdaaas.matches_gaze(msgs[0], gaze) is False


# ============================================================================
# Background Channel Tests
# ============================================================================

class TestBackgroundChannels:
    """Test awareness background_channels routing."""

    def test_doorbell_mode_delivers(self, env, eric):
        """Channel in background_channels with doorbell mode gets delivered."""
        env.set_gaze("AgentA", "irc", pm="eric")
        # #standup is already in background_channels as doorbell
        eric.say_in_channel("#standup", "hello", to_agents=["AgentA"])

        msgs = env.poll_messages("AgentA")
        assert len(msgs) == 1

        awareness = asdaaas.read_awareness("AgentA")
        mode = asdaaas.get_background_mode(msgs[0], awareness)
        assert mode == "doorbell"

    def test_pending_mode_queues(self, env, eric):
        """Channel not in background_channels falls to background_default (pending)."""
        env.set_gaze("AgentA", "irc", pm="eric")
        # #random is not in background_channels, default is "pending"
        eric.say_in_channel("#random", "hello", to_agents=["AgentA"])

        msgs = env.poll_messages("AgentA")
        assert len(msgs) == 1

        awareness = asdaaas.read_awareness("AgentA")
        mode = asdaaas.get_background_mode(msgs[0], awareness)
        assert mode == "pending"

    def test_add_channel_then_receive(self, env, eric):
        """Adding a channel to awareness makes it doorbell."""
        env.set_gaze("AgentA", "irc", pm="eric")
        env.set_awareness("AgentA", add="#meetingroom1", mode="doorbell")

        eric.say_in_channel("#meetingroom1", "meeting msg", to_agents=["AgentA"])

        msgs = env.poll_messages("AgentA")
        assert len(msgs) == 1

        awareness = asdaaas.read_awareness("AgentA")
        mode = asdaaas.get_background_mode(msgs[0], awareness)
        assert mode == "doorbell"

    def test_remove_channel_falls_to_default(self, env, eric):
        """Removing a channel from awareness makes it fall to default."""
        env.set_gaze("AgentA", "irc", pm="eric")
        env.set_awareness("AgentA", remove="#standup")

        eric.say_in_channel("#standup", "hello", to_agents=["AgentA"])

        msgs = env.poll_messages("AgentA")
        assert len(msgs) == 1

        awareness = asdaaas.read_awareness("AgentA")
        mode = asdaaas.get_background_mode(msgs[0], awareness)
        assert mode == "pending"  # default


# ============================================================================
# Multi-Agent Tests
# ============================================================================

class TestMultiAgent:
    """Test message routing with multiple agents."""

    def test_channel_message_reaches_both_agents(self, env, eric):
        """Both agents receive a channel message."""
        env.set_gaze("AgentA", "irc", room="#standup")
        env.set_gaze("AgentB", "irc", room="#standup")

        eric.say_in_channel("#standup", "hello everyone")

        msgs_a = env.poll_messages("AgentA")
        msgs_b = env.poll_messages("AgentB")
        assert len(msgs_a) == 1
        assert len(msgs_b) == 1

    def test_pm_only_reaches_target(self, env, eric):
        """PM to AgentA does NOT reach AgentB."""
        env.set_gaze("AgentA", "irc", pm="eric")
        env.set_gaze("AgentB", "irc", pm="eric")

        eric.pm("AgentA", "hey agent a only")

        msgs_a = env.poll_messages("AgentA")
        msgs_b = env.poll_messages("AgentB")
        assert len(msgs_a) == 1
        assert len(msgs_b) == 0

    def test_different_gaze_different_foreground(self, env, eric):
        """AgentA in #standup, AgentB in #meetingroom1. Message to #standup is foreground for A, not B."""
        env.set_gaze("AgentA", "irc", room="#standup")
        env.set_gaze("AgentB", "irc", room="#meetingroom1")

        eric.say_in_channel("#standup", "standup msg")

        msgs_a = env.poll_messages("AgentA")
        msgs_b = env.poll_messages("AgentB")

        gaze_a = asdaaas.read_gaze("AgentA")
        gaze_b = asdaaas.read_gaze("AgentB")

        assert len(msgs_a) == 1
        assert asdaaas.matches_gaze(msgs_a[0], gaze_a) is True

        assert len(msgs_b) == 1
        assert asdaaas.matches_gaze(msgs_b[0], gaze_b) is False

    def test_inter_agent_localmail(self, env):
        """AgentA sends localmail to AgentB."""
        env.inject_localmail("AgentA", "AgentB", "hey B, check this out")

        msgs = env.poll_messages("AgentB")
        assert len(msgs) == 1
        assert "check this out" in msgs[0].get("text", "")


# ============================================================================
# Multi-User Tests
# ============================================================================

class TestMultiUser:
    """Test with multiple users sending messages."""

    def test_two_users_same_channel(self, env, eric, gwen):
        """Two users in #standup, agent hears both."""
        env.set_gaze("AgentA", "irc", room="#standup")

        eric.say_in_channel("#standup", "eric here", to_agents=["AgentA"])
        gwen.say_in_channel("#standup", "gwen here", to_agents=["AgentA"])

        msgs = env.poll_messages("AgentA")
        assert len(msgs) == 2

    def test_two_users_pm_different_agents(self, env, eric, gwen):
        """Eric PMs AgentA, Gwen PMs AgentB — no cross-contamination."""
        env.set_gaze("AgentA", "irc", pm="eric")
        env.set_gaze("AgentB", "irc", pm="gwen")

        eric.pm("AgentA", "for A only")
        gwen.pm("AgentB", "for B only")

        msgs_a = env.poll_messages("AgentA")
        msgs_b = env.poll_messages("AgentB")

        assert len(msgs_a) == 1
        assert "for A only" in msgs_a[0].get("text", "")
        assert len(msgs_b) == 1
        assert "for B only" in msgs_b[0].get("text", "")

    def test_user_pm_does_not_reach_other_agent(self, env, eric, gwen):
        """Eric PMs AgentA. AgentB should NOT receive it."""
        eric.pm("AgentA", "secret message")

        msgs_b = env.poll_messages("AgentB")
        assert len(msgs_b) == 0


# ============================================================================
# Three-Agent Scenario Tests
# ============================================================================

class TestThreeAgentScenario:
    """Test realistic multi-agent scenarios."""

    def test_meeting_scenario(self, three_agent_env):
        """Three agents in a meeting room. Message reaches all three."""
        env = three_agent_env
        eric = MockUser("eric", env)

        env.set_gaze("AgentA", "irc", room="#meetingroom1")
        env.set_gaze("AgentB", "irc", room="#meetingroom1")
        env.set_gaze("AgentC", "irc", room="#meetingroom1")

        eric.say_in_channel("#meetingroom1", "meeting starts now",
                           to_agents=["AgentA", "AgentB", "AgentC"])

        for name in ["AgentA", "AgentB", "AgentC"]:
            msgs = env.poll_messages(name)
            assert len(msgs) == 1
            gaze = asdaaas.read_gaze(name)
            assert asdaaas.matches_gaze(msgs[0], gaze) is True

    def test_one_agent_leaves_meeting(self, three_agent_env):
        """AgentC leaves the meeting (gaze to pm:eric). Still gets doorbell if in awareness."""
        env = three_agent_env
        eric = MockUser("eric", env)

        env.set_gaze("AgentA", "irc", room="#meetingroom1")
        env.set_gaze("AgentB", "irc", room="#meetingroom1")
        env.set_gaze("AgentC", "irc", pm="eric")  # left the meeting
        env.set_awareness("AgentC", add="#meetingroom1", mode="doorbell")

        eric.say_in_channel("#meetingroom1", "important update",
                           to_agents=["AgentA", "AgentB", "AgentC"])

        # A and B: foreground
        for name in ["AgentA", "AgentB"]:
            msgs = env.poll_messages(name)
            gaze = asdaaas.read_gaze(name)
            assert asdaaas.matches_gaze(msgs[0], gaze) is True

        # C: background doorbell (not foreground)
        msgs_c = env.poll_messages("AgentC")
        gaze_c = asdaaas.read_gaze("AgentC")
        assert asdaaas.matches_gaze(msgs_c[0], gaze_c) is False
        awareness_c = asdaaas.read_awareness("AgentC")
        mode = asdaaas.get_background_mode(msgs_c[0], awareness_c)
        assert mode == "doorbell"
