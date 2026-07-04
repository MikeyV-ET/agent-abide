"""True e2e tests for fixture infrastructure via asdaaas_env.

Uses only the public file interface — no private asdaaas imports.
Exercises converted modules (localmail, interjection) through AsdaaasEnv.
"""

import json
import pytest
from pathlib import Path


class TestLocalmailViaFixture:
    """Verify localmail works through the hermetic fixture."""

    def test_inject_localmail_creates_inbox_file(self, asdaaas_env):
        """inject_localmail() writes a message to the agent's localmail inbox."""
        path = asdaaas_env.inject_localmail(from_agent="Sr", text="Hello from Sr")
        # File should exist in the inbox
        inbox = asdaaas_env.asdaaas_dir / "adapters" / "localmail" / "inbox"
        inbox_files = list(inbox.glob("*.json"))
        assert len(inbox_files) >= 1, "No inbox file created"

    def test_inject_localmail_message_content(self, asdaaas_env):
        """Localmail inbox file contains correct sender and text."""
        asdaaas_env.inject_localmail(from_agent="Q", text="Test message")
        inbox = asdaaas_env.asdaaas_dir / "adapters" / "localmail" / "inbox"
        inbox_files = list(inbox.glob("*.json"))
        assert len(inbox_files) >= 1, "No inbox file created"
        msg = json.loads(inbox_files[0].read_text())
        assert msg["from"] == "Q"
        assert msg["text"] == "Test message"
        assert msg["to"] == asdaaas_env.agent_name

    def test_inject_message_creates_adapter_inbox_file(self, asdaaas_env):
        """inject_message() writes to the specified adapter inbox."""
        path = asdaaas_env.inject_message("tui", "Hello from TUI", sender="eric")
        assert path.exists()
        msg = json.loads(path.read_text())
        assert msg["text"] == "Hello from TUI"
        assert msg["sender"] == "eric"
        assert msg["adapter"] == "tui"

    def test_inject_doorbell_creates_doorbell_file(self, asdaaas_env):
        """inject_doorbell() writes to the doorbells directory."""
        path = asdaaas_env.inject_doorbell("test_bell_1", text="Ding!")
        assert path.exists()
        bells = asdaaas_env.doorbells()
        assert len(bells) == 1
        assert bells[0]["id"] == "test_bell_1"
        assert bells[0]["text"] == "Ding!"

    def test_inject_command_creates_command_file(self, asdaaas_env):
        """inject_command() writes to the commands directory."""
        cmd = {"action": "delay", "seconds": 300}
        path = asdaaas_env.inject_command(cmd)
        assert path.exists()
        cmds = asdaaas_env.commands()
        assert len(cmds) == 1
        assert cmds[0]["action"] == "delay"
        assert cmds[0]["seconds"] == 300

    def test_outbox_empty_initially(self, asdaaas_env):
        """Outbox starts empty."""
        assert asdaaas_env.outbox("tui") == []

    def test_gaze_default(self, asdaaas_env):
        """Default gaze is TUI."""
        gaze = asdaaas_env.gaze()
        assert gaze["speech"]["target"] == "tui"

    def test_awareness_default(self, asdaaas_env):
        """Default awareness has TUI in direct_attach."""
        awareness = asdaaas_env.awareness()
        assert "tui" in awareness["direct_attach"]

    def test_hermetic_isolation(self, asdaaas_env):
        """Fixture uses tmp_path, not real ~/agents."""
        assert "tmp" in str(asdaaas_env.agents_home).lower() or \
               "/home/eric/agents" not in str(asdaaas_env.agents_home), \
            f"Fixture wrote to real agents dir: {asdaaas_env.agents_home}"

    def test_clear_doorbells(self, asdaaas_env):
        """clear_doorbells() removes all doorbell files."""
        asdaaas_env.inject_doorbell("bell_1")
        asdaaas_env.inject_doorbell("bell_2")
        assert len(asdaaas_env.doorbells()) == 2
        asdaaas_env.clear_doorbells()
        assert len(asdaaas_env.doorbells()) == 0

    def test_clear_outbox(self, asdaaas_env):
        """clear_outbox() removes all outbox files."""
        # Manually write an outbox file
        outbox = asdaaas_env.asdaaas_dir / "adapters" / "tui" / "outbox"
        (outbox / "resp_test.json").write_text(json.dumps({"text": "hi"}))
        assert len(asdaaas_env.outbox("tui")) == 1
        asdaaas_env.clear_outbox("tui")
        assert len(asdaaas_env.outbox("tui")) == 0


class TestInterjectionViaFixture:
    """Verify interjection queue/drain works through the hermetic fixture."""

    def test_inject_and_drain_interjection(self, asdaaas_env):
        """Queue an interjection, drain it back."""
        asdaaas_env.inject_interjection("STOP — do not proceed")
        messages = asdaaas_env.drain_interjections()
        assert len(messages) == 1
        assert "STOP" in messages[0]

    def test_drain_empty_queue(self, asdaaas_env):
        """Draining empty queue returns empty list."""
        messages = asdaaas_env.drain_interjections()
        assert messages == []

    def test_multiple_interjections(self, asdaaas_env):
        """Multiple interjections all drain in order."""
        asdaaas_env.inject_interjection("First message")
        asdaaas_env.inject_interjection("Second message")
        asdaaas_env.inject_interjection("Third message")
        messages = asdaaas_env.drain_interjections()
        assert len(messages) == 3

    def test_drain_is_destructive(self, asdaaas_env):
        """Draining consumes — second drain returns empty."""
        asdaaas_env.inject_interjection("One-shot message")
        first = asdaaas_env.drain_interjections()
        assert len(first) == 1
        second = asdaaas_env.drain_interjections()
        assert len(second) == 0


class TestTurnEngineScaffold:
    """Verify TurnEngine types are wirable through the fixture."""

    def test_make_engine(self, asdaaas_env):
        """make_engine() creates a TurnEngine with correct env."""
        engine = asdaaas_env.make_engine()
        assert engine.agent_name == "TestAgent"
        assert engine.env.agents_home == asdaaas_env.agents_home
        assert engine.agent_dir() == asdaaas_env.asdaaas_dir

    def test_gather_result_defaults(self, asdaaas_env):
        """GatherResult has sane defaults."""
        from turn_engine import GatherResult
        result = GatherResult()
        assert result.doorbells == []
        assert result.messages == []
        assert result.has_content is False

    def test_deliver_result_defaults(self, asdaaas_env):
        """DeliverResult has sane defaults."""
        from turn_engine import DeliverResult
        result = DeliverResult()
        assert result.speech == ""
        assert result.total_tokens == 0
        assert result.interjections_delivered == 0

    def test_post_turn_result_defaults(self, asdaaas_env):
        """PostTurnResult has sane defaults."""
        from turn_engine import PostTurnResult
        result = PostTurnResult()
        assert result.commands_processed == []
        assert result.continue_queued is False

    def test_engine_state_fields(self, asdaaas_env):
        """TurnEngine tracks per-session state."""
        engine = asdaaas_env.make_engine(context_window=100000)
        assert engine.context_window == 100000
        assert engine.total_tokens == 0
        assert engine.turns_since_compaction == 2
        assert engine.delay_until_event is False


class TestGatherPhase:
    """Test gather_pending() phase through the fixture.

    These tests inject input through the file interface and call
    engine.gather_pending() to verify GatherResult contents.
    """

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason="gather_pending doesn't thread env to poll_doorbells yet (S4 WIP)")
    async def test_gather_doorbells(self, asdaaas_env):
        """Injected doorbells appear in GatherResult."""
        asdaaas_env.inject_doorbell("bell_1", adapter="tui", sender="eric", text="Hey Trip")
        asdaaas_env.inject_doorbell("bell_2", adapter="tui", sender="eric", text="Second msg")
        engine = asdaaas_env.make_engine()
        result = await engine.gather_pending()
        assert len(result.doorbells) == 2
        assert result.has_content is True

    @pytest.mark.asyncio
    async def test_gather_no_content(self, asdaaas_env):
        """Empty workspace produces empty GatherResult."""
        engine = asdaaas_env.make_engine()
        result = await engine.gather_pending()
        assert result.doorbells == []
        assert result.messages == []
        assert result.has_content is False

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason="gather_pending doesn't thread env to poll_doorbells yet (S4 WIP)")
    async def test_gather_suppresses_redelivered_doorbells(self, asdaaas_env):
        """Doorbells already in last_delivered_bell_ids are suppressed."""
        asdaaas_env.inject_doorbell("bell_old", adapter="tui", text="Already seen")
        asdaaas_env.inject_doorbell("bell_new", adapter="tui", text="Fresh")
        engine = asdaaas_env.make_engine()
        engine.last_delivered_bell_ids = {"bell_old"}
        result = await engine.gather_pending()
        bell_ids = [b.get("id") for b in result.doorbells]
        assert "bell_new" in bell_ids
        assert "bell_old" not in bell_ids

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason="gather_pending doesn't thread env to poll_adapter_inboxes yet (S4 WIP)")
    async def test_gather_adapter_messages(self, asdaaas_env):
        """Messages in adapter inbox appear in GatherResult.messages."""
        asdaaas_env.inject_message("tui", "Hello from TUI", sender="eric")
        engine = asdaaas_env.make_engine()
        result = await engine.gather_pending()
        assert len(result.messages) >= 1
        assert any("Hello from TUI" in m.get("text", "") for m in result.messages)
        assert result.has_content is True
