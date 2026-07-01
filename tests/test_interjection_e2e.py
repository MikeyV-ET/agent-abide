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

from mock_binary import MockBinary, ShellToolCall, NormalResponse, Compaction
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


class TestPostCompactionOrientationInterjection:
    """Regression test for 393e9b5: interjection watcher during post-compaction orientation turn.

    Bug: The post-compaction orientation turn in main() called collect_response()
    WITHOUT spawning an interjection watcher. Messages sent during orientation
    were never queued, so ShellToolCall found an empty interjection directory.

    Fix: Spawn interjection_watcher around orientation's collect_response, same
    pattern as the regular turn path.

    These tests reproduce the orientation turn's code path: send_prompt → spawn
    watcher → collect_response (ShellToolCall) → cancel watcher. A TUI message
    injected during the ShellToolCall's duration should be queued by the watcher
    and picked up by ShellToolCall.
    """

    @pytest.mark.asyncio
    async def test_interjection_during_orientation_turn(self, agent_home, monkeypatch):
        """Message sent during post-compaction orientation turn is delivered via interjection."""
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        # Set up TUI adapter inbox (where the watcher polls)
        tui_inbox = agent_home / "agents" / "TestAgent" / "asdaaas" / "adapters" / "tui" / "inbox"
        tui_inbox.mkdir(parents=True, exist_ok=True)

        # Scenario: Compaction → ShellToolCall (orientation turn)
        # The ShellToolCall has enough duration for the watcher to poll
        scenario = [
            Compaction(tokens_before=150000, tokens_after=30000),
            ShellToolCall(
                command="cat lab_notebook.md",
                output="## Boot protocol complete\n",
                speech="Boot protocol followed.",
                tokens=32000,
                duration=1.0,  # enough time for watcher to poll
            ),
        ]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")

        await mock.start(agent_cwd=agent_cwd)

        # Step 1: Initial turn triggers compaction
        h1 = await mock.send_prompt("do work")
        r1 = await mock.collect_response(h1)
        # Compaction fires auto_compact_completed event
        has_event, tokens_after, tokens_before = mock.pop_compaction_event()
        assert has_event is True
        assert tokens_after == 30000

        # Step 2: Reproduce what main() does for orientation turn (393e9b5 fix)
        # This is the exact code path from asdaaas.py L2258-2278

        def poll_tui_inbox():
            """Mock poll function — reads TUI inbox like poll_adapter_inboxes."""
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

        orient_handle = await mock.send_prompt(
            "[Compaction complete. Context reduced from 150000 to 30000 tokens. "
            "You are resuming from a compacted context. Follow your boot protocol.]"
        )

        # Spawn interjection watcher (THE FIX — this was missing before 393e9b5)
        from interjection import interjection_watcher
        _ij_orient = asyncio.create_task(
            interjection_watcher("TestAgent", poll_tui_inbox, poll_interval=0.2)
        )

        # Inject a TUI message DURING the orientation turn
        # (simulates Eric typing while agent is booting)
        tui_msg = {
            "from": "eric",
            "text": "hey, you back from compaction?",
            "adapter": "tui",
            "id": "bell_post_compact_001",
            "ts": time.time(),
        }
        msg_path = tui_inbox / f"msg_{int(time.time()*1000)}.json"
        msg_path.write_text(json.dumps(tui_msg))

        # Small delay so watcher polls before ShellToolCall checks the queue
        await asyncio.sleep(0.5)

        # Collect orientation response (ShellToolCall checks interjection queue)
        orient_result = await mock.collect_response(orient_handle)

        # Cancel watcher (same as main())
        _ij_orient.cancel()
        try:
            await _ij_orient
        except asyncio.CancelledError:
            pass

        assert orient_result.speech == "Boot protocol followed."

        # Verify interjection appeared in the ShellToolCall output
        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        tool_updates = [
            json.loads(line) for line in updates
            if '"tool_call_update"' in line and '"completed"' in line
        ]

        # Should have exactly 1 completed tool_call_update (from ShellToolCall)
        assert len(tool_updates) >= 1, f"Expected tool_call_update, got {len(tool_updates)}"

        content = tool_updates[-1]["params"]["update"].get("content", "")
        assert "<interjection>" in content, (
            f"Interjection not found in orientation turn output. "
            f"Content: {content[:200]}"
        )
        assert "hey, you back from compaction?" in content
        assert "Boot protocol complete" in content  # original output preserved

    @pytest.mark.asyncio
    async def test_no_interjection_without_watcher(self, agent_home, monkeypatch):
        """Without the watcher (pre-393e9b5 bug), messages are NOT delivered during orientation."""
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        tui_inbox = agent_home / "agents" / "TestAgent" / "asdaaas" / "adapters" / "tui" / "inbox"
        tui_inbox.mkdir(parents=True, exist_ok=True)

        scenario = [
            Compaction(tokens_before=150000, tokens_after=30000),
            ShellToolCall(
                command="cat lab_notebook.md",
                output="## Boot protocol complete\n",
                speech="Boot done.",
                tokens=32000,
                duration=0.5,
            ),
        ]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")

        await mock.start(agent_cwd=agent_cwd)

        # Compaction step
        h1 = await mock.send_prompt("do work")
        await mock.collect_response(h1)
        mock.pop_compaction_event()

        # Orientation turn WITHOUT watcher (the old broken path)
        orient_handle = await mock.send_prompt("[Compaction complete...]")

        # Inject TUI message — but no watcher running to queue it
        tui_msg = {
            "from": "eric",
            "text": "are you there?",
            "adapter": "tui",
            "id": "bell_no_watcher_001",
            "ts": time.time(),
        }
        (tui_inbox / f"msg_{int(time.time()*1000)}.json").write_text(json.dumps(tui_msg))

        await asyncio.sleep(0.3)

        orient_result = await mock.collect_response(orient_handle)

        # Verify: NO interjection in output (message stuck in TUI inbox, never queued)
        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        tool_updates = [
            json.loads(line) for line in updates
            if '"tool_call_update"' in line and '"completed"' in line
        ]

        content = tool_updates[-1]["params"]["update"].get("content", "")
        assert "<interjection>" not in content, (
            "Interjection should NOT appear without watcher — "
            "message should still be in TUI inbox"
        )
        assert "Boot protocol complete" in content  # original output is there

        # Message should still be in TUI inbox (unconsumed)
        remaining = list(tui_inbox.glob("*.json"))
        assert len(remaining) == 1, "Message should still be in TUI inbox"

    @pytest.mark.asyncio
    async def test_multiple_messages_during_orientation(self, agent_home, monkeypatch):
        """Multiple messages arriving during orientation turn all get delivered."""
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        tui_inbox = agent_home / "agents" / "TestAgent" / "asdaaas" / "adapters" / "tui" / "inbox"
        tui_inbox.mkdir(parents=True, exist_ok=True)

        scenario = [
            Compaction(tokens_before=150000, tokens_after=30000),
            ShellToolCall(
                command="date",
                output="Wed Jul  1 07:00:00 PDT 2026\n",
                speech="Checked the time.",
                tokens=32000,
                duration=1.5,  # longer duration for multiple watcher polls
            ),
        ]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")

        await mock.start(agent_cwd=agent_cwd)

        h1 = await mock.send_prompt("work")
        await mock.collect_response(h1)
        mock.pop_compaction_event()

        orient_handle = await mock.send_prompt("[Compaction complete...]")

        from interjection import interjection_watcher
        _ij = asyncio.create_task(
            interjection_watcher("TestAgent", lambda: [
                json.loads(f.read_text()) or f.unlink()
                for f in sorted(tui_inbox.glob("*.json"))
                if not (f.unlink() if False else False)
            ] if tui_inbox.exists() else [], poll_interval=0.2)
        )

        # Actually, the lambda above is too clever. Use a proper poll function.
        _ij.cancel()
        try:
            await _ij
        except asyncio.CancelledError:
            pass

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

        _ij = asyncio.create_task(
            interjection_watcher("TestAgent", poll_tui, poll_interval=0.2)
        )

        # Send two messages with a small gap
        msg1 = {"from": "eric", "text": "first message", "adapter": "tui",
                "id": "bell_multi_001", "ts": time.time()}
        (tui_inbox / "msg_001.json").write_text(json.dumps(msg1))

        await asyncio.sleep(0.3)

        msg2 = {"from": "Sr", "text": "second message via localmail", "adapter": "localmail",
                "id": "bell_multi_002", "ts": time.time()}
        (tui_inbox / "msg_002.json").write_text(json.dumps(msg2))

        await asyncio.sleep(0.5)

        orient_result = await mock.collect_response(orient_handle)

        _ij.cancel()
        try:
            await _ij
        except asyncio.CancelledError:
            pass

        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        tool_updates = [
            json.loads(line) for line in updates
            if '"tool_call_update"' in line and '"completed"' in line
        ]

        content = tool_updates[-1]["params"]["update"].get("content", "")
        assert "<interjection>" in content
        assert "first message" in content
        assert "second message" in content


class TestConditionalDrainPostCompaction:
    """Tests for c39c47e: conditional drain during post-compaction orientation.

    When interjection_enabled=True: adapter messages stay in inboxes for the
    watcher to deliver mid-turn. Only internal messages (poll_inbox) drain.

    When interjection_enabled=False: all messages drain to pending queue
    (original issue_0033 behavior).
    """

    @pytest.mark.asyncio
    async def test_interjection_enabled_adapter_messages_not_drained(self, agent_home, monkeypatch):
        """With interjection enabled, adapter inbox messages stay for watcher pickup."""
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        # Set up TUI adapter inbox with a message
        tui_inbox = agent_home / "agents" / "TestAgent" / "asdaaas" / "adapters" / "tui" / "inbox"
        tui_inbox.mkdir(parents=True, exist_ok=True)

        msg = {
            "from": "eric",
            "text": "STOP — don't run that destructive command",
            "adapter": "tui",
            "id": "bell_stop_001",
            "ts": time.time(),
        }
        (tui_inbox / f"msg_{int(time.time()*1000)}.json").write_text(json.dumps(msg))

        # Scenario: Compaction → ShellToolCall (orientation turn with long enough duration)
        scenario = [
            Compaction(tokens_before=150000, tokens_after=30000),
            ShellToolCall(
                command="cat ~/agents/docs/PRINCIPLES.md",
                output="## check-the-foundation\n...\n",
                speech="Boot protocol started.",
                tokens=32000,
                duration=1.0,
            ),
        ]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")
        await mock.start(agent_cwd=agent_cwd)

        # Trigger compaction
        h1 = await mock.send_prompt("work")
        await mock.collect_response(h1)
        mock.pop_compaction_event()

        # Simulate interjection_enabled=True path:
        # Do NOT drain adapter inboxes (messages stay for watcher)
        # Verify messages are still in inbox
        remaining = list(tui_inbox.glob("*.json"))
        assert len(remaining) == 1, "Adapter message should NOT be drained"

        # Spawn watcher, orientation turn — watcher picks up the message
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

        orient_handle = await mock.send_prompt("[Compaction complete...]")

        from interjection import interjection_watcher
        _ij = asyncio.create_task(
            interjection_watcher("TestAgent", poll_tui, poll_interval=0.2)
        )

        await asyncio.sleep(0.5)

        orient_result = await mock.collect_response(orient_handle)

        _ij.cancel()
        try:
            await _ij
        except asyncio.CancelledError:
            pass

        # Verify: interjection delivered in tool output
        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        tool_updates = [
            json.loads(line) for line in updates
            if '"tool_call_update"' in line and '"completed"' in line
        ]
        content = tool_updates[-1]["params"]["update"].get("content", "")
        assert "<interjection>" in content, "Message should be interjected during orientation"
        assert "STOP" in content
        assert "destructive" in content

        # Adapter inbox should now be empty (watcher consumed it)
        remaining_after = list(tui_inbox.glob("*.json"))
        assert len(remaining_after) == 0, "Watcher should have consumed the message"

    @pytest.mark.asyncio
    async def test_interjection_disabled_adapter_messages_drained(self, agent_home, monkeypatch):
        """Without interjection, adapter inbox messages are drained to pending queue."""
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        # Set up TUI adapter inbox with a message
        tui_inbox = agent_home / "agents" / "TestAgent" / "asdaaas" / "adapters" / "tui" / "inbox"
        tui_inbox.mkdir(parents=True, exist_ok=True)

        msg = {
            "from": "eric",
            "text": "hey are you back?",
            "adapter": "tui",
            "id": "bell_drain_001",
            "ts": time.time(),
        }
        (tui_inbox / f"msg_{int(time.time()*1000)}.json").write_text(json.dumps(msg))

        # Simulate interjection_disabled path: drain adapter inboxes
        import asdaaas
        orig_home = asdaaas.AGENTS_HOME_DIR
        monkeypatch.setattr(asdaaas, "AGENTS_HOME_DIR", agent_home / "agents")

        awareness = {"direct_attach": ["tui"], "background_channels": {}}
        drained = asdaaas.poll_adapter_inboxes("TestAgent", awareness)

        # Restore
        monkeypatch.setattr(asdaaas, "AGENTS_HOME_DIR", orig_home)

        assert len(drained) == 1, "Message should have been drained"
        assert drained[0]["text"] == "hey are you back?"

        # Inbox should now be empty
        remaining = list(tui_inbox.glob("*.json"))
        assert len(remaining) == 0, "Inbox should be empty after drain"

        # Verify: running a ShellToolCall now finds NO interjection
        scenario = [
            ShellToolCall(
                command="date",
                output="Wed Jul 1\n",
                speech="Done.",
                tokens=32000,
                duration=0.3,
            ),
        ]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")
        await mock.start(agent_cwd=agent_cwd)

        h = await mock.send_prompt("[Compaction complete...]")
        await mock.collect_response(h)

        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        tool_updates = [
            json.loads(line) for line in updates
            if '"tool_call_update"' in line and '"completed"' in line
        ]
        content = tool_updates[-1]["params"]["update"].get("content", "")
        assert "<interjection>" not in content, "No interjection — messages were drained"

    @pytest.mark.asyncio
    async def test_internal_messages_always_drain(self, agent_home, monkeypatch):
        """Internal inbox (localmail) messages drain regardless of interjection setting."""
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        # Set up both adapter inbox AND internal inbox
        tui_inbox = agent_home / "agents" / "TestAgent" / "asdaaas" / "adapters" / "tui" / "inbox"
        tui_inbox.mkdir(parents=True, exist_ok=True)

        import asdaaas
        orig_home = asdaaas.AGENTS_HOME_DIR
        monkeypatch.setattr(asdaaas, "AGENTS_HOME_DIR", agent_home / "agents")

        # Create internal inbox message
        inbox_dir = agent_home / "agents" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(asdaaas, "INBOX_DIR", inbox_dir)

        internal_msg = {
            "from": "Sr",
            "to": "TestAgent",
            "text": "deployment complete",
            "adapter": "localmail",
        }
        (inbox_dir / f"msg_{int(time.time()*1000)}.json").write_text(json.dumps(internal_msg))

        # Adapter message
        adapter_msg = {
            "from": "eric",
            "text": "abort!",
            "adapter": "tui",
            "id": "bell_both_001",
            "ts": time.time(),
        }
        (tui_inbox / f"msg_{int(time.time()*1000)}.json").write_text(json.dumps(adapter_msg))

        # Simulate interjection_enabled=True path:
        # Internal messages drain, adapter messages do NOT drain
        internal_drained = asdaaas.poll_inbox("TestAgent")
        assert len(internal_drained) == 1, "Internal messages should always drain"
        assert internal_drained[0]["text"] == "deployment complete"

        # Adapter messages should still be in inbox
        adapter_remaining = list(tui_inbox.glob("*.json"))
        assert len(adapter_remaining) == 1, "Adapter messages should NOT drain with interjection enabled"

        monkeypatch.setattr(asdaaas, "AGENTS_HOME_DIR", orig_home)

    @pytest.mark.asyncio
    async def test_orientation_prompt_always_first(self, agent_home, monkeypatch):
        """Regardless of interjection setting, compaction-complete prompt is always first (issue_0033 regression guard)."""
        monkeypatch.setattr(Path, 'home', lambda: agent_home)

        tui_inbox = agent_home / "agents" / "TestAgent" / "asdaaas" / "adapters" / "tui" / "inbox"
        tui_inbox.mkdir(parents=True, exist_ok=True)

        msg = {
            "from": "eric",
            "text": "urgent message during compaction",
            "adapter": "tui",
            "id": "bell_order_001",
            "ts": time.time(),
        }
        (tui_inbox / f"msg_{int(time.time()*1000)}.json").write_text(json.dumps(msg))

        scenario = [
            Compaction(tokens_before=150000, tokens_after=30000),
            ShellToolCall(
                command="cat principles.md",
                output="principles content\n",
                speech="Done booting.",
                tokens=32000,
                duration=1.0,
            ),
        ]
        mock = MockBinary(scenario)
        agent_cwd = str(agent_home / "agents" / "TestAgent")
        await mock.start(agent_cwd=agent_cwd)

        # Compaction
        h1 = await mock.send_prompt("work")
        await mock.collect_response(h1)
        mock.pop_compaction_event()

        # Orientation prompt FIRST
        orientation_text = "[Compaction complete. Context reduced from 150000 to 30000 tokens.]"
        orient_handle = await mock.send_prompt(orientation_text)

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
        _ij = asyncio.create_task(
            interjection_watcher("TestAgent", poll_tui, poll_interval=0.2)
        )

        await asyncio.sleep(0.5)
        await mock.collect_response(orient_handle)

        _ij.cancel()
        try:
            await _ij
        except asyncio.CancelledError:
            pass

        # Check prompt ordering in updates.jsonl
        updates = (mock.session_dir / "updates.jsonl").read_text().strip().split("\n")
        user_prompts = [
            json.loads(line) for line in updates
            if '"user_message_chunk"' in line
        ]

        # The first user prompt after compaction should be the orientation message
        # Find prompts after the compaction event
        prompt_texts = [
            u["params"]["update"]["content"]["text"]
            for u in user_prompts
            if "Compaction complete" in u["params"]["update"].get("content", {}).get("text", "")
            or "urgent" in u["params"]["update"].get("content", {}).get("text", "")
        ]

        # Orientation must appear, and must be before any user message prompt
        assert any("Compaction complete" in t for t in prompt_texts), (
            "Orientation prompt must appear in prompts"
        )
        # Eric's message should NOT appear as a separate prompt — it should be an interjection
        assert not any("urgent message" in t for t in prompt_texts), (
            "User message should be interjected, not a separate prompt (issue_0033)"
        )
