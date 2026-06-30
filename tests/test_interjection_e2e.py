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


class TestFullPipelineE2E:
    """Full pipeline: adapter inbox → interjection_watcher → queue → ShellToolCall → stdout.

    Simulates a multi-tool-call turn where a message arrives mid-turn
    via TUI adapter inbox and gets interjected into a later shell call's output.
    """

    @pytest.mark.asyncio
    async def test_mid_turn_message_interjected(self, agent_home, monkeypatch):
        """Message arrives in TUI inbox during multi-tool-call turn.

        Flow:
        1. MockBinary has two ShellToolCalls (simulating multi-tool turn)
        2. First ShellToolCall runs clean (no messages yet)
        3. During first call, TUI message drops into adapter inbox
        4. interjection_watcher polls inbox → queues to interjection dir
        5. Second ShellToolCall picks up the interjection
        6. Verify message appears in second tool_call_update content
        """
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        # Set up TUI adapter inbox
        tui_inbox = agent_home / "agents" / "TestAgent" / "asdaaas" / "adapters" / "tui" / "inbox"
        tui_inbox.mkdir(parents=True, exist_ok=True)

        # Multi-tool-call scenario: first call is fast, second has longer duration
        # to give the watcher time to poll between calls
        scenario = [
            ShellToolCall(command="echo step1", output="step1 output\n",
                         speech="", tokens=5000, duration=0.5),
            ShellToolCall(command="echo step2", output="step2 output\n",
                         speech="Done with both.", tokens=5000, duration=0.5),
        ]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")

        await mock.start(agent_cwd=agent_cwd)

        # Define a mock poll function that reads TUI inbox (same pattern as asdaaas)
        def poll_tui_inbox():
            msgs = []
            if tui_inbox.exists():
                for f in sorted(tui_inbox.glob("*.json")):
                    try:
                        msg = json.loads(f.read_text())
                        msgs.append(msg)
                        f.unlink()
                    except (json.JSONDecodeError, OSError):
                        pass
            return msgs

        # Start the interjection watcher (polls every 0.3s for test speed)
        from interjection import interjection_watcher
        watcher_task = asyncio.create_task(
            interjection_watcher("TestAgent", poll_tui_inbox, poll_interval=0.3)
        )

        try:
            # Turn 1: first shell call — should be clean
            await mock.send_prompt("do two things")

            # Run first tool call
            # We need to process both tool calls, but inject a TUI message between them.
            # MockBinary processes one step per collect_response call,
            # so we call collect_response twice.
            result1 = await mock.collect_response(handle=1)

            # Drop a TUI message into the inbox (simulating Eric typing in the TUI)
            tui_msg = {
                "from": "eric",
                "text": "hey trip, how's it going?",
                "adapter": "tui",
                "id": "bell_tui_test_001",
                "ts": time.time(),
            }
            msg_path = tui_inbox / f"msg_{int(time.time()*1000)}.json"
            msg_path.write_text(json.dumps(tui_msg))

            # Wait for watcher to poll and queue the interjection
            await asyncio.sleep(0.8)

            # Run second tool call — should pick up the interjection
            await mock.send_prompt("continue")
            result2 = await mock.collect_response(handle=2)

        finally:
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass

        # Parse all updates.jsonl entries
        updates_text = (mock.session_dir / "updates.jsonl").read_text().strip()
        all_updates = [json.loads(line) for line in updates_text.split("\n")]
        tool_updates = [
            u for u in all_updates
            if u["params"]["update"].get("sessionUpdate") == "tool_call_update"
            and u["params"]["update"].get("status") == "completed"
        ]

        assert len(tool_updates) == 2, f"Expected 2 tool_call_updates, got {len(tool_updates)}"

        # First tool call output should be clean (no interjection)
        content1 = tool_updates[0]["params"]["update"].get("content", "")
        assert "<interjection>" not in content1
        assert "step1 output" in content1

        # Second tool call output should have the interjection
        content2 = tool_updates[1]["params"]["update"].get("content", "")
        assert "<interjection>" in content2
        assert "hey trip, how's it going?" in content2
        assert "step2 output" in content2
        assert "[system: messages arrived during your tool call]" in content2

    @pytest.mark.asyncio
    async def test_interjection_files_consumed_after_delivery(self, agent_home, monkeypatch):
        """After ShellToolCall delivers an interjection, the queue dir is empty."""
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        tui_inbox = agent_home / "agents" / "TestAgent" / "asdaaas" / "adapters" / "tui" / "inbox"
        tui_inbox.mkdir(parents=True, exist_ok=True)
        intj_dir = agent_home / "agents" / "TestAgent" / "asdaaas" / "interjections"

        scenario = [
            ShellToolCall(command="echo work", output="work done\n",
                         speech="", tokens=5000, duration=0.3),
            ShellToolCall(command="echo more", output="more done\n",
                         speech="All done.", tokens=5000, duration=0.3),
        ]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")
        await mock.start(agent_cwd=agent_cwd)

        def poll_tui():
            msgs = []
            if tui_inbox.exists():
                for f in sorted(tui_inbox.glob("*.json")):
                    try:
                        msgs.append(json.loads(f.read_text()))
                        f.unlink()
                    except (json.JSONDecodeError, OSError):
                        pass
            return msgs

        from interjection import interjection_watcher
        watcher = asyncio.create_task(
            interjection_watcher("TestAgent", poll_tui, poll_interval=0.2)
        )

        try:
            await mock.send_prompt("work")
            await mock.collect_response(handle=1)

            # Queue a message
            msg = {"from": "eric", "text": "checking in", "adapter": "tui",
                   "id": "bell_consume_test", "ts": time.time()}
            (tui_inbox / "msg_consume.json").write_text(json.dumps(msg))
            await asyncio.sleep(0.5)

            # Verify message is in interjection queue before second call
            queued = list(intj_dir.glob("*.txt"))
            assert len(queued) > 0, "Watcher should have queued the message"

            await mock.send_prompt("more")
            await mock.collect_response(handle=2)

            # After delivery, queue should be empty
            remaining = list(intj_dir.glob("*.txt"))
            assert remaining == [], f"Expected empty queue, got {remaining}"

        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_delivery_tracked_in_interjection_log(self, agent_home, monkeypatch):
        """Bonus: verify asdaaas can track delivery via interjection_log.txt.

        The real BASH_ENV hook logs deliveries. ShellToolCall doesn't write
        the log (that's the hook's job), but we can verify the watcher's
        queue_interjection was called by checking the interjection dir was
        populated, and that the message made it through to tool output.
        """
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        tui_inbox = agent_home / "agents" / "TestAgent" / "asdaaas" / "adapters" / "tui" / "inbox"
        tui_inbox.mkdir(parents=True, exist_ok=True)

        scenario = [
            ShellToolCall(command="cat file.txt", output="file contents\n",
                         speech="", tokens=5000, duration=0.3),
            ShellToolCall(command="echo done", output="done\n",
                         speech="Finished.", tokens=5000, duration=0.3),
        ]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")
        await mock.start(agent_cwd=agent_cwd)

        def poll_tui():
            msgs = []
            if tui_inbox.exists():
                for f in sorted(tui_inbox.glob("*.json")):
                    try:
                        msgs.append(json.loads(f.read_text()))
                        f.unlink()
                    except (json.JSONDecodeError, OSError):
                        pass
            return msgs

        from interjection import interjection_watcher, format_message_for_interjection
        watcher = asyncio.create_task(
            interjection_watcher("TestAgent", poll_tui, poll_interval=0.2)
        )

        try:
            await mock.send_prompt("read file")
            await mock.collect_response(handle=1)

            # Simulate message via TUI during the turn
            tui_msg = {
                "from": "eric",
                "text": "important update",
                "adapter": "tui",
                "id": "bell_track_test",
                "ts": time.time(),
            }
            (tui_inbox / "msg_track.json").write_text(json.dumps(tui_msg))
            await asyncio.sleep(0.5)

            await mock.send_prompt("continue")
            await mock.collect_response(handle=2)

        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

        # Verify the formatted message made it through
        updates_text = (mock.session_dir / "updates.jsonl").read_text().strip()
        all_updates = [json.loads(line) for line in updates_text.split("\n")]
        tool_updates = [
            u for u in all_updates
            if u["params"]["update"].get("sessionUpdate") == "tool_call_update"
            and u["params"]["update"].get("status") == "completed"
        ]

        # Second tool call should contain the formatted interjection
        content2 = tool_updates[1]["params"]["update"].get("content", "")
        assert "important update" in content2
        assert "eric" in content2
        assert "bell_track_test" in content2

        # Verify the message was formatted with the bell ID for acking
        # (format_message_for_interjection includes id= in the output)
        assert "id=bell_track_test" in content2
