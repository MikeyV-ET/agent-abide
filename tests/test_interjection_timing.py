"""Tests for interjection timing: message arrives mid-turn, must be picked up by subsequent tool calls.

Reproduces the failure from 2026-07-08 where Eric sent Jr a TUI message during
an active turn with 14+ bash tool calls, but the message was never interjected.
Instead it appeared as a continue doorbell after the turn ended (post-turn drain).

The test verifies: if the interjection_watcher is running alongside tool calls,
and a message arrives in the adapter inbox mid-turn, a subsequent ShellToolCall
picks it up via the interjection queue.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))

from mock_binary import MockBinary, ShellToolCall
from interjection import (
    interjection_watcher, interjection_dir,
    queue_interjection, drain_interjection_queue,
)


@pytest.fixture
def agent_home(tmp_path, monkeypatch):
    """Create mock agent home with adapter inbox and interjection dir."""
    agent_dir = tmp_path / "agents" / "TestAgent" / "asdaaas"
    agent_dir.mkdir(parents=True)
    (agent_dir / "commands").mkdir()
    (agent_dir / "interjections").mkdir()
    (agent_dir / "adapters" / "tui" / "inbox").mkdir(parents=True)
    from asdaaas_env import AsdaaasEnv
    test_env = AsdaaasEnv(agents_home=tmp_path / "agents")
    monkeypatch.setattr(AsdaaasEnv, "from_config", classmethod(lambda cls: test_env))
    return tmp_path


def write_tui_message(agent_home, text, msg_id="bell_test"):
    """Simulate TUI writing a message to the adapter inbox."""
    inbox = agent_home / "agents" / "TestAgent" / "asdaaas" / "adapters" / "tui" / "inbox"
    msg = {
        "from": "eric",
        "adapter": "tui",
        "text": text,
        "id": msg_id,
        "ts": time.time(),
    }
    msg_path = inbox / f"msg_{int(time.time() * 1000)}.json"
    msg_path.write_text(json.dumps(msg))
    return msg_path


def make_poll_fn(agent_home):
    """Create a poll function that reads TUI adapter inbox (like poll_adapter_inboxes)."""
    def poll():
        inbox = agent_home / "agents" / "TestAgent" / "asdaaas" / "adapters" / "tui" / "inbox"
        msgs = []
        if inbox.exists():
            for f in sorted(inbox.glob("*.json")):
                try:
                    msgs.append(json.loads(f.read_text()))
                    f.unlink()
                except (json.JSONDecodeError, OSError):
                    pass
        return msgs
    return poll


class TestInterjectionTiming:
    """Reproduce the Jr failure: message arrives mid-turn, watcher + tool calls running."""

    @pytest.mark.asyncio
    async def test_message_before_tool_call_is_interjected(self, agent_home, monkeypatch):
        """Message queued by watcher BEFORE tool call starts → interjected in tool output."""
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        scenario = [
            # Two tool calls: first is slow enough for watcher to poll
            ShellToolCall(command="echo first", output="first\n", speech="", duration=0.5),
            ShellToolCall(command="echo second", output="second\n", speech="Done.", duration=0.1),
        ]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")
        await mock.start(agent_cwd=agent_cwd)

        # Start the watcher (fast poll for testing)
        poll_fn = make_poll_fn(agent_home)
        watcher = asyncio.create_task(
            interjection_watcher("TestAgent", poll_fn, poll_interval=0.05)
        )

        # Write TUI message to inbox BEFORE sending prompt
        write_tui_message(agent_home, "any benefit to using subagents?")

        # Give watcher time to poll and queue the interjection
        await asyncio.sleep(0.15)

        # Now send prompt — tool calls should pick up the interjection
        await mock.send_prompt("work")
        result = await mock.collect_response(handle=1)

        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass

        # Check tool output for interjection
        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        tool_updates = [
            json.loads(line) for line in updates
            if '"tool_call_update"' in line and '"completed"' in line
        ]
        all_content = "".join(
            u["params"]["update"].get("content", "") for u in tool_updates
        )
        assert "<interjection>" in all_content, (
            "Message queued before tool call should be interjected"
        )
        assert "any benefit to using subagents?" in all_content

    @pytest.mark.asyncio
    async def test_message_during_tool_calls_is_interjected(self, agent_home, monkeypatch):
        """Message arrives DURING first tool call → picked up by second tool call.

        This is the exact scenario from Jr's failure: message arrives while
        tool calls are running, watcher polls, queues to interjections dir,
        next tool call picks it up.
        """
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        scenario = [
            # First tool call: long duration, message arrives during this
            ShellToolCall(command="cat big_file.py", output="file content\n", speech="", duration=0.8),
            # Second tool call: should pick up the interjection
            ShellToolCall(command="echo check", output="check\n", speech="", duration=0.1),
            # Third tool call: should be clean
            ShellToolCall(command="echo done", output="done\n", speech="Done.", duration=0.1),
        ]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")
        await mock.start(agent_cwd=agent_cwd)

        poll_fn = make_poll_fn(agent_home)
        watcher = asyncio.create_task(
            interjection_watcher("TestAgent", poll_fn, poll_interval=0.05)
        )

        # Send prompt to start the turn
        handle = mock.send_prompt("work on the refactor")

        # Wait for first tool call to start, then inject message
        await asyncio.sleep(0.3)
        write_tui_message(agent_home, "any benefit to using subagents?")

        # Give watcher time to poll
        await asyncio.sleep(0.2)

        await handle  # ensure prompt is sent
        result = await mock.collect_response(handle=1)

        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass

        # Check each tool call's output
        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        tool_updates = [
            json.loads(line) for line in updates
            if '"tool_call_update"' in line and '"completed"' in line
        ]

        # At least one tool call should have the interjection
        interjected = any(
            "<interjection>" in u["params"]["update"].get("content", "")
            for u in tool_updates
        )
        assert interjected, (
            "Message arriving mid-turn should be interjected in a subsequent tool call. "
            "Tool outputs: " + str([u["params"]["update"].get("content", "")[:100] for u in tool_updates])
        )

        # Verify the message content is there
        all_content = "".join(
            u["params"]["update"].get("content", "") for u in tool_updates
        )
        assert "any benefit to using subagents?" in all_content

        # Drain should find nothing (message was consumed by hook)
        leftover = drain_interjection_queue("TestAgent")
        assert len(leftover) == 0, (
            f"Interjection queue should be empty after tool calls consumed it, "
            f"but found {len(leftover)} leftover(s)"
        )

    @pytest.mark.asyncio
    async def test_watcher_error_doesnt_crash_silently(self, agent_home, monkeypatch):
        """If the watcher's poll_fn throws, it should not die silently.

        Current code only catches CancelledError. Any other exception kills
        the watcher task without logging — messages never get queued.
        """
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        call_count = [0]
        def crashing_poll():
            call_count[0] += 1
            if call_count[0] == 2:
                raise OSError("disk full")
            return []

        watcher = asyncio.create_task(
            interjection_watcher("TestAgent", crashing_poll, poll_interval=0.05)
        )

        await asyncio.sleep(0.25)

        # Check: is the watcher still alive after the error?
        assert watcher.done(), (
            "Watcher should have died from unhandled OSError"
        )

        # The watcher died — this is the bug. It should have caught the error
        # and continued polling (or at least logged it).
        # This test documents the current behavior; the fix would be to add
        # exception handling around the poll loop body.
        exc = watcher.exception()
        assert isinstance(exc, OSError), (
            f"Watcher should have died with OSError, got: {exc}"
        )

    @pytest.mark.asyncio
    async def test_message_after_all_tool_calls_goes_to_drain(self, agent_home, monkeypatch):
        """Message arriving after all tool calls complete → leftover in drain."""
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        scenario = [
            ShellToolCall(command="echo fast", output="fast\n", speech="Done.", duration=0.1),
        ]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")
        await mock.start(agent_cwd=agent_cwd)

        poll_fn = make_poll_fn(agent_home)
        watcher = asyncio.create_task(
            interjection_watcher("TestAgent", poll_fn, poll_interval=0.05)
        )

        # Send prompt and let tool call complete
        await mock.send_prompt("work")
        result = await mock.collect_response(handle=1)

        # Message arrives AFTER tool calls finish but BEFORE watcher is cancelled
        write_tui_message(agent_home, "late message")
        await asyncio.sleep(0.15)  # let watcher poll

        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass

        # Tool output should be clean
        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        tool_updates = [
            json.loads(line) for line in updates
            if '"tool_call_update"' in line and '"completed"' in line
        ]
        content = tool_updates[0]["params"]["update"].get("content", "")
        assert "<interjection>" not in content, "Late message should NOT be in tool output"

        # Drain should find the leftover
        leftover = drain_interjection_queue("TestAgent")
        assert len(leftover) == 1, "Late message should be in drain queue"
        assert "late message" in leftover[0]
