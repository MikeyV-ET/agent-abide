"""E2E tests for BASH_ENV interjection via MockBinary.

Tests the full pipeline: message queued → ShellToolCall checks queue →
interjection block prepended to tool output.
"""

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Add core to path
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))

from mock_binary import MockBinary, ShellToolCall, NormalResponse
from interjection import queue_interjection, interjection_dir, drain_interjection_queue


@pytest.fixture
def agent_home(tmp_path):
    """Create a mock agent home directory structure."""
    agent_dir = tmp_path / "agents" / "TestAgent" / "asdaaas"
    agent_dir.mkdir(parents=True)
    (agent_dir / "commands").mkdir()
    (agent_dir / "interjections").mkdir()
    return tmp_path


@pytest.fixture
def mock_binary_with_shell(agent_home):
    """Create a MockBinary with a ShellToolCall step, pointed at test agent dir."""
    scenario = [
        ShellToolCall(command="echo hello", output="hello\n", speech="Done.", duration=0.1),
    ]
    mock = MockBinary(scenario)
    return mock, agent_home


class TestShellToolCallNoInterjection:
    """ShellToolCall with empty interjection queue — output unchanged."""

    @pytest.mark.asyncio
    async def test_clean_output(self, mock_binary_with_shell):
        mock, agent_home = mock_binary_with_shell
        agent_cwd = str(agent_home / "agents" / "TestAgent")

        await mock.start(agent_cwd=agent_cwd)
        await mock.send_prompt("run something")

        result = await mock.collect_response(handle=1)
        assert result.speech == "Done."

        # Check updates.jsonl for tool_call_update content
        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        tool_updates = [
            json.loads(line) for line in updates
            if '"tool_call_update"' in line
        ]
        assert len(tool_updates) == 1
        content = tool_updates[0]["params"]["update"].get("content", "")
        assert content == "hello\n"
        assert "<interjection>" not in content

    @pytest.mark.asyncio
    async def test_no_interjection_dir(self, agent_home):
        """ShellToolCall works even if interjection dir doesn't exist."""
        # Remove the interjection dir
        intj_dir = agent_home / "agents" / "TestAgent" / "asdaaas" / "interjections"
        intj_dir.rmdir()

        scenario = [ShellToolCall(output="ok\n", speech="Fine.", duration=0.05)]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")

        await mock.start(agent_cwd=agent_cwd)
        await mock.send_prompt("test")
        result = await mock.collect_response(handle=1)
        assert result.speech == "Fine."


class TestShellToolCallWithInterjection:
    """ShellToolCall picks up queued interjection messages."""

    @pytest.mark.asyncio
    async def test_single_message(self, mock_binary_with_shell):
        mock, agent_home = mock_binary_with_shell
        agent_cwd = str(agent_home / "agents" / "TestAgent")

        await mock.start(agent_cwd=agent_cwd)

        # Queue a message before the tool call
        intj_dir = agent_home / "agents" / "TestAgent" / "asdaaas" / "interjections"
        msg_file = intj_dir / "interject_test_001.txt"
        msg_file.write_text("[eric (via tui) (id=bell_abc, ts=test)] hey trip\n")

        await mock.send_prompt("run something")
        result = await mock.collect_response(handle=1)

        # Verify interjection appears in tool output
        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        tool_updates = [
            json.loads(line) for line in updates
            if '"tool_call_update"' in line
        ]
        content = tool_updates[0]["params"]["update"].get("content", "")
        assert "<interjection>" in content
        assert "hey trip" in content
        assert "hello\n" in content  # original output still present

    @pytest.mark.asyncio
    async def test_multiple_messages(self, agent_home):
        """Multiple queued messages all appear in interjection block."""
        scenario = [ShellToolCall(output="result\n", speech="OK.", duration=0.05)]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")

        await mock.start(agent_cwd=agent_cwd)

        intj_dir = agent_home / "agents" / "TestAgent" / "asdaaas" / "interjections"
        (intj_dir / "interject_001.txt").write_text("[eric (via tui)] message one\n")
        (intj_dir / "interject_002.txt").write_text("[localmail from Sr] message two\n")

        await mock.send_prompt("test")
        result = await mock.collect_response(handle=1)

        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        tool_updates = [json.loads(l) for l in updates if '"tool_call_update"' in l]
        content = tool_updates[0]["params"]["update"].get("content", "")

        assert "<interjection>" in content
        assert "</interjection>" in content
        assert "message one" in content
        assert "message two" in content
        assert "result\n" in content

    @pytest.mark.asyncio
    async def test_messages_consumed(self, mock_binary_with_shell):
        """Interjection files are deleted after pickup."""
        mock, agent_home = mock_binary_with_shell
        agent_cwd = str(agent_home / "agents" / "TestAgent")

        await mock.start(agent_cwd=agent_cwd)

        intj_dir = agent_home / "agents" / "TestAgent" / "asdaaas" / "interjections"
        (intj_dir / "interject_consume_test.txt").write_text("consume me\n")

        await mock.send_prompt("test")
        await mock.collect_response(handle=1)

        # Files should be gone
        remaining = list(intj_dir.glob("*.txt"))
        assert remaining == []

    @pytest.mark.asyncio
    async def test_interjection_format_matches_hook(self, mock_binary_with_shell):
        """Output format matches interjection_hook.sh: <interjection>\\n[system: ...]\\n...\\n</interjection>"""
        mock, agent_home = mock_binary_with_shell
        agent_cwd = str(agent_home / "agents" / "TestAgent")

        await mock.start(agent_cwd=agent_cwd)

        intj_dir = agent_home / "agents" / "TestAgent" / "asdaaas" / "interjections"
        (intj_dir / "interject_fmt.txt").write_text("[test msg]\n")

        await mock.send_prompt("test")
        await mock.collect_response(handle=1)

        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        tool_updates = [json.loads(l) for l in updates if '"tool_call_update"' in l]
        content = tool_updates[0]["params"]["update"].get("content", "")

        # Verify structure matches hook output
        assert content.startswith("<interjection>\n")
        assert "[system: messages arrived during your tool call]\n" in content
        assert "</interjection>\n" in content
        # Original output comes after the interjection block
        assert content.endswith("hello\n")


class TestShellToolCallIntegration:
    """Integration: queue_interjection() → ShellToolCall pickup."""

    @pytest.mark.asyncio
    async def test_queue_then_shell_pickup(self, agent_home, monkeypatch):
        """Messages queued via queue_interjection() are picked up by ShellToolCall."""
        # Monkeypatch Path.home to use our tmp agent structure
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        scenario = [ShellToolCall(output="ls output\n", speech="Listed.", duration=0.1)]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")

        await mock.start(agent_cwd=agent_cwd)

        # Use the real queue_interjection function
        queue_interjection("TestAgent", "[Sr (via localmail)] check this out\n")

        await mock.send_prompt("ls")
        result = await mock.collect_response(handle=1)

        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        tool_updates = [json.loads(l) for l in updates if '"tool_call_update"' in l]
        content = tool_updates[0]["params"]["update"].get("content", "")

        assert "<interjection>" in content
        assert "check this out" in content
        assert "ls output" in content

    @pytest.mark.asyncio
    async def test_second_shell_call_clean(self, agent_home, monkeypatch):
        """After first ShellToolCall consumes messages, second ShellToolCall is clean."""
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        scenario = [
            ShellToolCall(output="first\n", speech="One.", duration=0.05),
            ShellToolCall(output="second\n", speech="Two.", duration=0.05),
        ]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")

        await mock.start(agent_cwd=agent_cwd)

        queue_interjection("TestAgent", "mid-turn message\n")

        # First tool call picks up the message
        await mock.send_prompt("cmd1")
        await mock.collect_response(handle=1)

        # Second tool call should be clean
        await mock.send_prompt("cmd2")
        await mock.collect_response(handle=2)

        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        tool_updates = [json.loads(l) for l in updates if '"tool_call_update"' in l]

        # First has interjection, second doesn't
        assert "<interjection>" in tool_updates[0]["params"]["update"].get("content", "")
        assert "<interjection>" not in tool_updates[1]["params"]["update"].get("content", "")

    @pytest.mark.asyncio
    async def test_tmp_files_ignored(self, agent_home):
        """In-progress .tmp files are not picked up (atomic write safety)."""
        scenario = [ShellToolCall(output="ok\n", speech="OK.", duration=0.05)]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")

        await mock.start(agent_cwd=agent_cwd)

        intj_dir = agent_home / "agents" / "TestAgent" / "asdaaas" / "interjections"
        (intj_dir / "interject_partial.tmp").write_text("not ready yet\n")

        await mock.send_prompt("test")
        await mock.collect_response(handle=1)

        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        tool_updates = [json.loads(l) for l in updates if '"tool_call_update"' in l]
        content = tool_updates[0]["params"]["update"].get("content", "")

        assert "<interjection>" not in content
        assert "not ready yet" not in content


class TestShellToolCallToolEvents:
    """Verify correct updates.jsonl event structure."""

    @pytest.mark.asyncio
    async def test_writes_tool_call_and_update(self, mock_binary_with_shell):
        """ShellToolCall writes both tool_call (Pending) and tool_call_update (Completed)."""
        mock, agent_home = mock_binary_with_shell
        agent_cwd = str(agent_home / "agents" / "TestAgent")

        await mock.start(agent_cwd=agent_cwd)
        await mock.send_prompt("test")
        await mock.collect_response(handle=1)

        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        parsed = [json.loads(l) for l in updates]

        tool_calls = [p for p in parsed if p["params"]["update"].get("sessionUpdate") == "tool_call"]
        tool_updates = [p for p in parsed if p["params"]["update"].get("sessionUpdate") == "tool_call_update"]

        assert len(tool_calls) == 1
        assert tool_calls[0]["params"]["update"]["title"] == "run_terminal_command"
        assert tool_calls[0]["params"]["update"]["rawInput"]["command"] == "echo hello"

        assert len(tool_updates) == 1
        assert tool_updates[0]["params"]["update"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_on_tool_call_callback(self, mock_binary_with_shell):
        """on_tool_call callback is invoked with 'run_terminal_command'."""
        mock, agent_home = mock_binary_with_shell
        agent_cwd = str(agent_home / "agents" / "TestAgent")

        await mock.start(agent_cwd=agent_cwd)
        await mock.send_prompt("test")

        called_with = []
        result = await mock.collect_response(
            handle=1,
            on_tool_call=lambda name: called_with.append(name),
        )

        assert called_with == ["run_terminal_command"]
