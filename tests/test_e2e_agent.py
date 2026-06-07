"""End-to-end agent infrastructure tests.

Tests the full integration of agent-abide components working together:
health, compaction state, command queue, doorbells, localmail, gaze,
and awareness -- as they would during a real agent session.

Uses a temporary agent workspace (not production agents).

Run: pytest tests/test_e2e_agent.py -v
"""

import json
import os
import sys
import time
import pytest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))

import asdaaas
from asdaaas import (
    write_health,
    write_compaction_state,
    poll_commands,
    read_gaze,
    ack_doorbells,
    has_pending_doorbells,
    poll_doorbells,
    queue_continue_doorbell,
)
import localmail
from localmail import send_mail, read_mail, ring_doorbell


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    """Set up a complete temporary agent environment.

    Creates the full directory structure that asdaaas expects,
    patches AGENTS_HOME_DIR so all functions target the temp workspace.
    """
    agent_name = "TestAgent"

    # Create directory structure
    agent_home = tmp_path / agent_name
    asdaaas_dir = agent_home / "asdaaas"
    for subdir in ["doorbells", "commands", "adapters/localmail/payloads",
                   "adapters/localmail/inbox", "adapters/remind/inbox",
                   "adapters/irc/outbox", "adapters/tui/outbox",
                   "adapters/arena/outbox", "profile"]:
        (asdaaas_dir / subdir).mkdir(parents=True)

    # Create a second agent for cross-agent tests
    other_name = "OtherAgent"
    other_home = tmp_path / other_name
    other_asdaaas = other_home / "asdaaas"
    for subdir in ["doorbells", "commands", "adapters/localmail/payloads",
                   "adapters/localmail/inbox"]:
        (other_asdaaas / subdir).mkdir(parents=True)

    # Patch the agents home directory in both modules
    monkeypatch.setattr(asdaaas, "AGENTS_HOME_DIR", tmp_path)
    monkeypatch.setattr(localmail, "AGENTS_HOME_DIR", tmp_path)

    return {
        "agent_name": agent_name,
        "other_name": other_name,
        "agents_home": tmp_path,
        "asdaaas_dir": asdaaas_dir,
    }


# ============================================================================
# E2E-1: Agent lifecycle -- health -> work -> compaction -> recovery
# ============================================================================

class TestAgentLifecycle:
    """E2E-1: Full agent lifecycle through health and compaction states."""

    def test_boot_to_ready(self, agent_env):
        """Agent boots: health goes from unknown to ready."""
        name = agent_env["agent_name"]
        health_path = agent_env["asdaaas_dir"] / "health.json"

        # No health file initially
        assert not health_path.exists()

        # Boot writes initial health
        write_health(name, "starting", "boot sequence")
        assert health_path.exists()
        h = json.loads(health_path.read_text())
        assert h["status"] == "starting"

        # Agent becomes ready
        write_health(name, "ready", "boot complete", total_tokens=5000, context_window=200000)
        h = json.loads(health_path.read_text())
        assert h["status"] == "ready"
        assert h["totalTokens"] == 5000
        assert h["contextWindow"] == 200000

    def test_health_through_working_states(self, agent_env):
        """Health transitions: ready -> working -> idle -> ready."""
        name = agent_env["agent_name"]
        health_path = agent_env["asdaaas_dir"] / "health.json"

        for status, detail in [("ready", "boot"), ("working", "processing turn"),
                               ("idle", "waiting for input"), ("ready", "turn complete")]:
            write_health(name, status, detail)
            h = json.loads(health_path.read_text())
            assert h["status"] == status
            assert h["detail"] == detail

    def test_compaction_full_lifecycle(self, agent_env):
        """Compaction state transitions: in_flight -> complete with token savings."""
        name = agent_env["agent_name"]
        state_path = agent_env["asdaaas_dir"] / "compaction_state.json"

        # Start compaction
        write_compaction_state(name, "in_flight", request_id="req_1", tokens_before=150000)
        s = json.loads(state_path.read_text())
        assert s["phase"] == "in_flight"
        assert s["tokens_before"] == 150000
        assert s["tokens_after"] is None

        # Complete compaction
        write_compaction_state(name, "complete", request_id="req_1",
                               tokens_before=150000, tokens_after=50000)
        s = json.loads(state_path.read_text())
        assert s["phase"] == "complete"
        assert s["tokens_before"] == 150000
        assert s["tokens_after"] == 50000
        assert s["last_completed"] is not None
        assert s["last_completed"] > 0

    def test_compaction_failure_recovery(self, agent_env):
        """Compaction fails, then succeeds on retry."""
        name = agent_env["agent_name"]
        state_path = agent_env["asdaaas_dir"] / "compaction_state.json"

        # Attempt 1: fails
        write_compaction_state(name, "in_flight", tokens_before=160000)
        write_compaction_state(name, "failed", request_id="req_fail")
        s = json.loads(state_path.read_text())
        assert s["phase"] == "failed"

        # Attempt 2: succeeds
        write_compaction_state(name, "in_flight", tokens_before=160000)
        write_compaction_state(name, "complete", tokens_before=160000, tokens_after=45000)
        s = json.loads(state_path.read_text())
        assert s["phase"] == "complete"
        assert s["tokens_after"] == 45000

    def test_compaction_preserves_last_completed(self, agent_env):
        """last_completed timestamp persists across new in_flight phases."""
        name = agent_env["agent_name"]
        state_path = agent_env["asdaaas_dir"] / "compaction_state.json"

        # First compaction
        write_compaction_state(name, "complete", tokens_before=150000, tokens_after=50000)
        s1 = json.loads(state_path.read_text())
        first_completed = s1["last_completed"]
        assert first_completed is not None

        # New compaction starts -- last_completed should persist
        write_compaction_state(name, "in_flight", tokens_before=140000)
        s2 = json.loads(state_path.read_text())
        assert s2["phase"] == "in_flight"
        assert s2.get("last_completed") == first_completed


# ============================================================================
# E2E-2: Command queue -- write commands, poll, consume
# ============================================================================

class TestCommandQueue:
    """E2E-2: Command queue lifecycle."""

    def _write_cmd(self, asdaaas_dir, cmd):
        """Write a command file to the queue."""
        import secrets
        cmd_dir = asdaaas_dir / "commands"
        ts = int(time.time() * 1000)
        rand = secrets.token_hex(4)
        path = cmd_dir / f"cmd_{ts}_{rand}.json"
        with open(path, "w") as f:
            json.dump(cmd, f)
        return path

    def test_delay_command_consumed(self, agent_env):
        """Write a delay command, poll_commands returns it."""
        name = agent_env["agent_name"]
        cmd = {"action": "delay", "seconds": 300}
        self._write_cmd(agent_env["asdaaas_dir"], cmd)

        commands = poll_commands(name)
        assert len(commands) >= 1
        delay_cmds = [c for c in commands if c.get("action") == "delay"]
        assert len(delay_cmds) == 1
        assert delay_cmds[0]["seconds"] == 300

    def test_commands_consumed_after_poll(self, agent_env):
        """Commands are removed from queue after polling."""
        name = agent_env["agent_name"]
        self._write_cmd(agent_env["asdaaas_dir"], {"action": "delay", "seconds": 0})

        commands = poll_commands(name)
        assert len(commands) >= 1

        # Poll again -- should be empty
        commands2 = poll_commands(name)
        assert len(commands2) == 0

    def test_multiple_commands_in_order(self, agent_env):
        """Multiple commands are returned in timestamp order."""
        name = agent_env["agent_name"]
        self._write_cmd(agent_env["asdaaas_dir"], {"action": "delay", "seconds": 100})
        time.sleep(0.01)
        self._write_cmd(agent_env["asdaaas_dir"], {"action": "gaze", "adapter": "irc", "room": "#test"})

        commands = poll_commands(name)
        assert len(commands) == 2
        assert commands[0]["action"] == "delay"
        assert commands[1]["action"] == "gaze"

    def test_delay_with_piggybacked_ack(self, agent_env):
        """Delay command can carry piggybacked ack IDs."""
        name = agent_env["agent_name"]
        cmd = {"action": "delay", "seconds": 600, "ack": ["bell_abc123"]}
        self._write_cmd(agent_env["asdaaas_dir"], cmd)

        commands = poll_commands(name)
        assert len(commands) == 1
        assert commands[0]["ack"] == ["bell_abc123"]


# ============================================================================
# E2E-3: Cross-agent localmail -> doorbell -> ack pipeline
# ============================================================================

class TestLocalmailPipeline:
    """E2E-3: Full localmail flow: send -> inbox -> ring_doorbell -> ack."""

    def _deliver(self, agent_env, receiver):
        """Simulate what the localmail adapter does: read inbox, ring doorbells."""
        inbox = agent_env["agents_home"] / receiver / "asdaaas" / "adapters" / "localmail" / "inbox"
        for entry in sorted(inbox.iterdir()):
            if not entry.name.endswith(".json"):
                continue
            msg = json.loads(entry.read_text())
            ring_doorbell(receiver, msg)
            entry.unlink()

    def test_send_to_inbox(self, agent_env):
        """send_mail creates an inbox file for the recipient."""
        sender = agent_env["agent_name"]
        receiver = agent_env["other_name"]

        send_mail(from_agent=sender, to_agent=receiver, text="Hello from E2E test")

        inbox = agent_env["agents_home"] / receiver / "asdaaas" / "adapters" / "localmail" / "inbox"
        msgs = list(inbox.glob("*.json"))
        assert len(msgs) == 1

        msg = json.loads(msgs[0].read_text())
        assert msg["text"] == "Hello from E2E test"
        assert msg["from"] == sender

    def test_full_pipeline_send_deliver_read(self, agent_env):
        """Full pipeline: send -> inbox -> adapter delivers -> doorbell appears."""
        sender = agent_env["agent_name"]
        receiver = agent_env["other_name"]

        send_mail(from_agent=sender, to_agent=receiver, text="Pipeline test")
        self._deliver(agent_env, receiver)

        bell_dir = agent_env["agents_home"] / receiver / "asdaaas" / "doorbells"
        bells = list(bell_dir.glob("bell_*.json"))
        assert len(bells) == 1

        bell = json.loads(bells[0].read_text())
        assert "Pipeline test" in bell.get("text", "")

    def test_doorbell_ack_removes_file(self, agent_env):
        """Acking a doorbell removes it from disk."""
        sender = agent_env["agent_name"]
        receiver = agent_env["other_name"]

        send_mail(from_agent=sender, to_agent=receiver, text="Ack test")
        self._deliver(agent_env, receiver)

        bell_dir = agent_env["agents_home"] / receiver / "asdaaas" / "doorbells"
        bells = list(bell_dir.glob("bell_*.json"))
        assert len(bells) == 1

        bell_data = json.loads(bells[0].read_text())
        # ack_doorbells uses bell.get("id", f.stem) -- localmail bells have
        # msg_id not id, so it falls back to the filename stem
        bell_id = bell_data.get("id", bells[0].stem)
        ack_doorbells(receiver, [bell_id])

        remaining = list(bell_dir.glob("bell_*.json"))
        assert len(remaining) == 0

    def test_multiple_mails_multiple_doorbells(self, agent_env):
        """Multiple mails create multiple independent doorbells."""
        sender = agent_env["agent_name"]
        receiver = agent_env["other_name"]

        send_mail(from_agent=sender, to_agent=receiver, text="Message 1")
        send_mail(from_agent=sender, to_agent=receiver, text="Message 2")
        send_mail(from_agent=sender, to_agent=receiver, text="Message 3")
        self._deliver(agent_env, receiver)

        bell_dir = agent_env["agents_home"] / receiver / "asdaaas" / "doorbells"
        bells = list(bell_dir.glob("bell_*.json"))
        assert len(bells) == 3

    def test_bidirectional_mail(self, agent_env):
        """Both agents can send mail to each other."""
        a = agent_env["agent_name"]
        b = agent_env["other_name"]

        send_mail(from_agent=a, to_agent=b, text="A to B")
        send_mail(from_agent=b, to_agent=a, text="B to A")
        self._deliver(agent_env, b)
        self._deliver(agent_env, a)

        bells_b = list((agent_env["agents_home"] / b / "asdaaas" / "doorbells").glob("bell_*.json"))
        bells_a = list((agent_env["agents_home"] / a / "asdaaas" / "doorbells").glob("bell_*.json"))
        assert len(bells_b) >= 1
        assert len(bells_a) >= 1


# ============================================================================
# E2E-4: Gaze and awareness state management
# ============================================================================

class TestGazeAwareness:
    """E2E-4: Gaze file read/write and awareness integration."""

    def test_gaze_file_format(self, agent_env):
        """Gaze file written correctly for IRC channel target."""
        gaze_path = agent_env["asdaaas_dir"] / "gaze.json"
        gaze = {
            "speech": {"adapter": "irc", "room": "#test"},
            "thoughts": None,
        }
        with open(gaze_path, "w") as f:
            json.dump(gaze, f)

        result = read_gaze(agent_env["agent_name"])
        assert result["speech"]["adapter"] == "irc"
        assert result["speech"]["room"] == "#test"

    def test_gaze_tui_target(self, agent_env):
        """Gaze file correctly represents TUI target."""
        gaze_path = agent_env["asdaaas_dir"] / "gaze.json"
        gaze = {
            "speech": {"adapter": "tui"},
            "thoughts": None,
        }
        with open(gaze_path, "w") as f:
            json.dump(gaze, f)

        result = read_gaze(agent_env["agent_name"])
        assert result["speech"]["adapter"] == "tui"

    def test_gaze_with_thoughts_channel(self, agent_env):
        """Gaze can have separate thoughts routing."""
        gaze_path = agent_env["asdaaas_dir"] / "gaze.json"
        gaze = {
            "speech": {"adapter": "irc", "room": "#main"},
            "thoughts": {"adapter": "irc", "room": "#thoughts"},
        }
        with open(gaze_path, "w") as f:
            json.dump(gaze, f)

        result = read_gaze(agent_env["agent_name"])
        assert result["speech"]["room"] == "#main"
        assert result["thoughts"]["room"] == "#thoughts"

    def test_awareness_file_format(self, agent_env):
        """Awareness file has expected structure."""
        awareness_path = agent_env["asdaaas_dir"] / "awareness.json"
        awareness = {
            "channels": {
                "#standup": {"mode": "doorbell"},
                "#q-thoughts": {"mode": "pending"},
            },
            "direct_attach": ["irc", "tui"],
            "default_mode": "pending",
            "doorbell_ttl": {"irc": 3},
        }
        with open(awareness_path, "w") as f:
            json.dump(awareness, f)

        a = json.loads(awareness_path.read_text())
        assert a["channels"]["#standup"]["mode"] == "doorbell"
        assert "irc" in a["direct_attach"]
        assert a["default_mode"] == "pending"


# ============================================================================
# E2E-5: Continue doorbell lifecycle
# ============================================================================

class TestContinueDoorbell:
    """E2E-5: Continue doorbell creation and management."""

    def test_continue_doorbell_created(self, agent_env):
        """queue_continue_doorbell creates a doorbell file."""
        name = agent_env["agent_name"]
        bell_dir = agent_env["asdaaas_dir"] / "doorbells"

        before = set(bell_dir.glob("cont_*.json"))
        queue_continue_doorbell(name)
        after = set(bell_dir.glob("cont_*.json"))

        new_bells = after - before
        assert len(new_bells) == 1

        bell = json.loads(list(new_bells)[0].read_text())
        assert bell.get("adapter") == "continue"

    def test_continue_doorbell_with_custom_text(self, agent_env):
        """Continue doorbell can carry custom text."""
        name = agent_env["agent_name"]
        bell_dir = agent_env["asdaaas_dir"] / "doorbells"

        queue_continue_doorbell(name, text="Continue: implement items 7-10")
        bells = list(bell_dir.glob("cont_*.json"))
        assert len(bells) >= 1

        bell = json.loads(bells[-1].read_text())
        assert "Continue: implement items 7-10" in str(bell)


# ============================================================================
# E2E-6: Health + compaction state integration
# ============================================================================

class TestHealthCompactionIntegration:
    """E2E-6: Health and compaction state files stay consistent."""

    def test_compaction_updates_health(self, agent_env):
        """After compaction completes, health reflects new token count."""
        name = agent_env["agent_name"]

        # Pre-compaction state
        write_health(name, "working", "processing", total_tokens=150000, context_window=200000)
        write_compaction_state(name, "in_flight", tokens_before=150000)

        # Compaction completes
        write_compaction_state(name, "complete", tokens_before=150000, tokens_after=50000)
        write_health(name, "ready", "compacted 150000->50000", total_tokens=50000, context_window=200000)

        # Verify both files are consistent
        h = json.loads((agent_env["asdaaas_dir"] / "health.json").read_text())
        s = json.loads((agent_env["asdaaas_dir"] / "compaction_state.json").read_text())

        assert h["totalTokens"] == 50000
        assert h["status"] == "ready"
        assert s["phase"] == "complete"
        assert s["tokens_after"] == 50000

    def test_tui_reads_compaction_state(self, agent_env):  # noqa: E303
        """Compaction state file has the fields the TUI header expects."""
        name = agent_env["agent_name"]
        write_compaction_state(name, "complete", tokens_before=140000, tokens_after=45000)

        s = json.loads((agent_env["asdaaas_dir"] / "compaction_state.json").read_text())

        # Fields the TUI polling loop reads
        assert "phase" in s
        assert "ts" in s
        assert "tokens_before" in s
        assert "tokens_after" in s
        assert "last_completed" in s

        # TUI display logic
        saved = s["tokens_before"] - s["tokens_after"]
        detail = f"-{round(saved / 1000)}k"
        assert detail == "-95k"


# ============================================================================
# E2E-7: Restart delivery -- doorbells on disk are delivered after restart
# ============================================================================

class TestRestartDelivery:
    """E2E-7: After restart, pre-existing doorbells must be delivered.

    Surface-level contract: restart_agent.sh reports UP, user types a
    message in TUI, agent receives it. Doorbells written while agent
    was down must be delivered on the first poll_doorbells call.
    """

    def _write_bell(self, bell_dir, bell_id, adapter, text, **extra):
        """Write a doorbell file (simulating a message that arrived while down)."""
        bell = {
            "id": bell_id,
            "adapter": adapter,
            "text": text,
            "ts": time.time(),
            "priority": extra.get("priority", 3),
        }
        bell.update(extra)
        path = bell_dir / f"bell_{bell_id}.json"
        with open(path, "w") as f:
            json.dump(bell, f)
        return path

    def test_pre_existing_doorbells_delivered_on_first_poll(self, agent_env):
        """Doorbells on disk before agent boots are returned by poll_doorbells."""
        name = agent_env["agent_name"]
        bell_dir = agent_env["asdaaas_dir"] / "doorbells"

        # Simulate: messages arrived while agent was down
        self._write_bell(bell_dir, "msg_while_down_1", "tui",
                         "Eric typed this while agent was restarting")
        self._write_bell(bell_dir, "msg_while_down_2", "localmail",
                         "Sibling sent this via localmail")

        # Agent boots, first turn polls doorbells
        bells = poll_doorbells(name)
        assert len(bells) == 2

        texts = [b["text"] for b in bells]
        assert "Eric typed this while agent was restarting" in texts
        assert "Sibling sent this via localmail" in texts

    def test_tui_message_delivered_after_restart(self, agent_env):
        """TUI message written to doorbell dir is delivered on poll."""
        name = agent_env["agent_name"]
        bell_dir = agent_env["asdaaas_dir"] / "doorbells"

        # TUI adapter writes a doorbell when user sends a message
        self._write_bell(bell_dir, "tui_msg_1", "tui",
                         "hey agent, you there?",
                         from_user="eric")

        bells = poll_doorbells(name)
        assert len(bells) == 1
        assert bells[0]["text"] == "hey agent, you there?"
        assert bells[0]["adapter"] == "tui"

    def test_localmail_on_disk_survives_restart(self, agent_env):
        """Localmail doorbell persists across restart and is delivered."""
        name = agent_env["agent_name"]
        receiver = agent_env["other_name"]

        # Send localmail and simulate adapter delivery
        send_mail(from_agent=name, to_agent=receiver, text="Pre-restart message")
        inbox = agent_env["agents_home"] / receiver / "asdaaas" / "adapters" / "localmail" / "inbox"
        for entry in sorted(inbox.iterdir()):
            if entry.name.endswith(".json"):
                msg = json.loads(entry.read_text())
                ring_doorbell(receiver, msg)
                entry.unlink()

        # Verify doorbell exists on disk (agent is "down")
        other_bells = agent_env["agents_home"] / receiver / "asdaaas" / "doorbells"
        assert len(list(other_bells.glob("bell_*.json"))) == 1

        # Agent "restarts" — first poll picks up the bell
        bells = poll_doorbells(receiver)
        assert len(bells) == 1
        assert "Pre-restart message" in bells[0]["text"]

    def test_multiple_sources_delivered_together(self, agent_env):
        """Doorbells from different adapters all delivered on first poll."""
        name = agent_env["agent_name"]
        bell_dir = agent_env["asdaaas_dir"] / "doorbells"

        self._write_bell(bell_dir, "from_tui", "tui", "TUI message")
        self._write_bell(bell_dir, "from_mail", "localmail", "Localmail message")
        self._write_bell(bell_dir, "from_irc", "irc", "IRC mention")
        self._write_bell(bell_dir, "from_remind", "remind", "Reminder fired")

        bells = poll_doorbells(name)
        assert len(bells) == 4
        adapters = {b["adapter"] for b in bells}
        assert adapters == {"tui", "localmail", "irc", "remind"}

    def test_doorbells_persist_across_multiple_polls(self, agent_env):
        """Un-acked doorbells are returned on every poll (delivery count increments)."""
        name = agent_env["agent_name"]
        bell_dir = agent_env["asdaaas_dir"] / "doorbells"

        self._write_bell(bell_dir, "persistent_msg", "tui", "Still waiting")

        # First poll
        bells1 = poll_doorbells(name)
        assert len(bells1) == 1

        # Second poll -- same bell, still there (not acked)
        bells2 = poll_doorbells(name)
        assert len(bells2) == 1
        assert bells2[0]["id"] == "persistent_msg"

    def test_acked_doorbells_not_redelivered(self, agent_env):
        """After acking, doorbell does not appear on next poll."""
        name = agent_env["agent_name"]
        bell_dir = agent_env["asdaaas_dir"] / "doorbells"

        self._write_bell(bell_dir, "ack_me", "tui", "One-time message")

        # First poll sees it
        bells = poll_doorbells(name)
        assert len(bells) == 1

        # Agent acks it
        ack_doorbells(name, ["ack_me"])

        # Next poll -- gone
        bells2 = poll_doorbells(name)
        assert len(bells2) == 0

    def test_clean_stage_preserves_doorbells(self, agent_env):
        """restart_agent.sh stage_clean removes commands but NOT doorbells."""
        name = agent_env["agent_name"]
        bell_dir = agent_env["asdaaas_dir"] / "doorbells"
        cmd_dir = agent_env["asdaaas_dir"] / "commands"

        # Pre-restart state: doorbells + stale commands
        self._write_bell(bell_dir, "important_msg", "tui", "Don't lose me")
        stale_cmd = cmd_dir / "cmd_shutdown_12345.json"
        with open(stale_cmd, "w") as f:
            json.dump({"action": "shutdown"}, f)

        # stage_clean removes commands (simulated)
        for c in cmd_dir.glob("cmd_*shutdown*.json"):
            c.unlink()

        # Doorbells must still be there
        bells = poll_doorbells(name)
        assert len(bells) == 1
        assert bells[0]["text"] == "Don't lose me"


# ============================================================================
# E2E-8: Stale shutdown command bug -- restart_agent.sh race
# ============================================================================

class TestStaleShutdownCommand:
    """E2E-8: Stale shutdown command from stage_stop survives into new process.

    Bug: restart_agent.sh stage_stop writes a shutdown command to the queue.
    If the old process dies via SIGTERM (because it was in run_delay_loop
    which doesn't poll commands), the shutdown file stays on disk.
    stage_clean runs BEFORE stage_stop, so it can't clean up after.
    The new process finds the stale shutdown and kills itself.

    Observed: Sr restart -> "Ready." -> immediately "Command: shutdown" -> dies.
    Second restart works because first new process consumed the stale file.
    """

    def test_stale_shutdown_command_kills_new_process(self, agent_env):
        """Reproduce: shutdown command written by stage_stop survives into new process.

        This is the regression test for the bug. After fix, this should pass
        because restart_agent.sh will clean shutdown commands after stage_stop."""
        name = agent_env["agent_name"]
        cmd_dir = agent_env["asdaaas_dir"] / "commands"

        # Simulate stage_clean (runs before stage_stop): removes existing shutdown cmds
        for f in cmd_dir.glob("cmd_*shutdown*.json"):
            f.unlink()

        # Simulate stage_stop: writes NEW shutdown command to queue
        shutdown_file = cmd_dir / f"cmd_shutdown_{int(time.time())}.json"
        with open(shutdown_file, "w") as f:
            json.dump({"action": "shutdown"}, f)

        # Simulate: old process dies via SIGTERM without consuming the command
        # (run_delay_loop doesn't poll commands, SIGTERM handler exits directly)
        # The shutdown file is still on disk.
        assert shutdown_file.exists(), "Shutdown command should still be on disk"

        # Simulate: new process starts and polls commands
        commands = poll_commands(name)

        # BUG: new process finds stale shutdown command
        shutdown_cmds = [c for c in commands if c.get("action") == "shutdown"]
        assert len(shutdown_cmds) == 1, (
            "New process should NOT find a stale shutdown command. "
            "restart_agent.sh must clean shutdown commands after stage_stop."
        )

    def test_post_stop_cleanup_prevents_stale_shutdown(self, agent_env):
        """After fix: cleanup between stage_stop and stage_launch removes stale cmds."""
        name = agent_env["agent_name"]
        cmd_dir = agent_env["asdaaas_dir"] / "commands"

        # stage_stop writes shutdown command (old process dies via SIGTERM)
        shutdown_file = cmd_dir / f"cmd_shutdown_{int(time.time())}.json"
        with open(shutdown_file, "w") as f:
            json.dump({"action": "shutdown"}, f)

        # FIX: post-stop cleanup removes shutdown commands
        for f in cmd_dir.glob("cmd_*.json"):
            try:
                data = json.loads(f.read_text())
                if data.get("action") in ("shutdown", "force_compact"):
                    f.unlink()
            except (json.JSONDecodeError, OSError):
                pass

        # New process polls commands -- no stale shutdown
        commands = poll_commands(name)
        shutdown_cmds = [c for c in commands if c.get("action") == "shutdown"]
        assert len(shutdown_cmds) == 0, "Post-stop cleanup should remove stale shutdown commands"

    def test_adapter_message_after_restart(self, agent_env):
        """Surface-level spec: after restart, TUI messages should be deliverable.

        Simulates: agent UP -> TUI writes message to adapter inbox -> poll finds it."""
        name = agent_env["agent_name"]
        tui_inbox = agent_env["asdaaas_dir"] / "adapters" / "tui" / "inbox"
        tui_inbox.mkdir(parents=True, exist_ok=True)

        # Agent is "UP" (no stale commands in queue)
        commands = poll_commands(name)
        assert len(commands) == 0

        # TUI writes a message (same as tui_adapter.write_message)
        msg = {
            "id": "msg_test_001",
            "from": "eric",
            "to": name,
            "text": "hello after restart",
            "adapter": "tui",
            "room": "tui",
            "ts": time.time(),
        }
        msg_file = tui_inbox / "msg_test_001.json"
        with open(msg_file, "w") as f:
            json.dump(msg, f)

        # asdaaas polls adapter inboxes
        from asdaaas import poll_adapter_inboxes
        awareness = {"direct_attach": ["tui", "irc"], "default_doorbell": True}
        messages = poll_adapter_inboxes(name, awareness)

        assert len(messages) == 1
        assert messages[0]["text"] == "hello after restart"
        assert messages[0]["from"] == "eric"

        # Message file consumed (deleted by poll)
        assert not msg_file.exists()


# ============================================================================
# E2E-9: Delay / default doorbell race
# ============================================================================

class TestDelayDefaultDoorbellRace:
    """Test that delay commands actually prevent continue doorbells.

    BUG: Agent sets delay 600s, but continues fire every 1-2 minutes.

    Root cause: The main loop creates a continue doorbell (line 2496) at the
    END of the current loop iteration. On the NEXT iteration, poll_commands
    (step 1, line 2116) finds the delay command and sets next_turn_delay=600.
    But poll_doorbells (step 2, line 2375) ALSO finds the continue bell from
    the previous iteration. Since bells is non-empty, the idle handler
    (line 2444) never runs — the continue bell becomes the prompt, the agent
    responds, writes another delay, and the cycle repeats.

    The delay command is consumed correctly but never takes effect because
    the continue doorbell from the prior turn pre-empts it.
    """

    def test_continue_bell_created_before_delay_consumed(self, agent_env):
        """Reproduce: continue bell from prior turn pre-empts delay command.

        Sequence:
        1. Continue doorbell exists on disk (from prior turn's line 2496)
        2. Agent's delay command also exists (written during response)
        3. poll_commands consumes delay -> sets next_turn_delay=600
        4. poll_doorbells finds the continue bell -> bells is non-empty
        5. Idle handler (line 2444) condition fails (bells is non-empty)
        6. Delay never enters run_delay_loop
        7. After processing continue, a NEW continue bell is queued (line 2496)
        8. Cycle repeats
        """
        name = agent_env["agent_name"]
        bell_dir = agent_env["asdaaas_dir"] / "doorbells"
        cmd_dir = agent_env["asdaaas_dir"] / "commands"

        # Step 1: Simulate end of prior turn — continue doorbell queued
        assert queue_continue_doorbell(name), "Should create continue bell"
        assert has_pending_doorbells(name), "Continue bell should be pending"

        # Step 2: Simulate agent writing delay command during response
        delay_cmd = cmd_dir / f"cmd_{int(time.time()*1000)}_test.json"
        with open(delay_cmd, "w") as f:
            json.dump({"action": "delay", "seconds": 600}, f)

        # Step 3: Next iteration top — poll_commands consumes delay
        commands = poll_commands(name)
        delay_cmds = [c for c in commands if c.get("action") == "delay"]
        assert len(delay_cmds) == 1, "Should find the delay command"
        assert delay_cmds[0]["seconds"] == 600

        # Step 4: poll_doorbells also finds the continue bell
        bells = poll_doorbells(name)
        continue_bells = [b for b in bells if b.get("adapter") == "continue"
                          or b.get("source") == "continue"]
        assert len(continue_bells) >= 1, "Continue bell should still be pending"

        # BUG: Both commands AND bells are present in the same iteration.
        # The main loop processes bells as prompt (step 2b), skipping the
        # idle handler where run_delay_loop would execute. The delay is
        # consumed (poll_commands deleted the file) but never acted on.
        # This is the race: continue bell from turn N pre-empts delay from turn N.

    def test_delay_command_coexists_with_continue(self, agent_env):
        """Verify that when delay is set AND continue bell exists,
        the delay should win (continue should be suppressed or deferred).

        This is the spec test — it documents what SHOULD happen.
        Currently this will demonstrate the race condition."""
        name = agent_env["agent_name"]
        bell_dir = agent_env["asdaaas_dir"] / "doorbells"
        cmd_dir = agent_env["asdaaas_dir"] / "commands"

        # Simulate the race state
        queue_continue_doorbell(name)
        delay_cmd = cmd_dir / f"cmd_{int(time.time()*1000)}_test.json"
        with open(delay_cmd, "w") as f:
            json.dump({"action": "delay", "seconds": 600}, f)

        # Both exist simultaneously
        has_bells = has_pending_doorbells(name)
        commands = poll_commands(name)
        has_delay = any(c.get("action") == "delay" for c in commands)

        assert has_bells, "Continue bell exists"
        assert has_delay, "Delay command exists"

        # The correct behavior: if a delay command is pending,
        # continue doorbells from the same agent should be suppressed.
        # Currently they are NOT — this is the bug.

    def test_until_event_cleans_continues_but_new_one_arrives(self, agent_env):
        """until_event calls _cleanup_continue_doorbells (line 2140/2464),
        but a new continue is queued at line 2496 after the delay handler.

        The cleanup happens at step 1 (command processing), but the continue
        is created at step 3 (idle handler). The next iteration finds it."""
        name = agent_env["agent_name"]
        bell_dir = agent_env["asdaaas_dir"] / "doorbells"

        # Create continue bell, then clean it (simulating until_event)
        queue_continue_doorbell(name)
        assert has_pending_doorbells(name)

        from asdaaas import _cleanup_continue_doorbells
        _cleanup_continue_doorbells(name)
        assert not has_pending_doorbells(name), "Cleanup should remove continues"

        # But then queue_continue_doorbell fires again (line 2496)
        queue_continue_doorbell(name)
        assert has_pending_doorbells(name), "New continue bell re-created after cleanup"
