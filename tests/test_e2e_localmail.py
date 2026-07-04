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
