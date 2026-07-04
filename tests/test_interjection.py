"""Tests for mid-turn message interjection system.

Tests two components:
1. Hook script (core/interjection_hook.sh) — BASH_ENV hook that drains queue
2. Queue function (core/interjection.py) — Python API to enqueue messages

Run: cd ~/projects/agent-abide && python -m pytest tests/test_interjection.py -v
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Add core to path for queue function tests
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

HOOK_SCRIPT = Path(__file__).resolve().parent.parent / "core" / "interjection_hook.sh"


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def _patch_asdaaas_env(tmp_path, monkeypatch):
    """S1 transition: patch AsdaaasEnv.from_config to use tmp_path/agents."""
    from asdaaas_env import AsdaaasEnv
    test_env = AsdaaasEnv(agents_home=tmp_path / "agents")
    monkeypatch.setattr(AsdaaasEnv, "from_config", classmethod(lambda cls: test_env))


@pytest.fixture
def agent_env(tmp_path):
    """Set up a temp agent home with interjection queue directory.

    Creates the directory structure that mimics ~/agents/testagent/asdaaas/interjections/
    and returns a dict with paths and env vars for running the hook.
    """
    agent_name = "testagent"
    interject_dir = tmp_path / agent_name / "asdaaas" / "interjections"
    interject_dir.mkdir(parents=True)

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "AGENT_NAME": agent_name,
    }

    # Hook resolves ~/agents/$AGENT_NAME/... so we need agents/ under HOME
    agents_dir = tmp_path / "agents" / agent_name / "asdaaas" / "interjections"
    agents_dir.mkdir(parents=True)

    return {
        "agent_name": agent_name,
        "interject_dir": agents_dir,
        "home": tmp_path,
        "env": env,
    }


def run_hook(env, extra_cmd="echo done"):
    """Run a bash command with BASH_ENV pointing to the hook script.

    Returns subprocess.CompletedProcess with stdout/stderr.
    """
    result = subprocess.run(
        ["/bin/bash", "-c", extra_cmd],
        env={**env, "BASH_ENV": str(HOOK_SCRIPT)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result


def queue_file(interject_dir, filename, content):
    """Write a .txt file directly into the interjection queue directory."""
    path = interject_dir / filename
    path.write_text(content)
    return path


# ============================================================================
# Hook script tests (subprocess — BASH_ENV sourcing)
# ============================================================================

class TestHookEmptyQueue:
    """Hook behavior when no messages are queued."""

    def test_empty_queue_no_output(self, agent_env):
        """BASH_ENV hook produces zero interjection output when no messages queued."""
        result = run_hook(agent_env["env"])
        # Only the command's own output should appear
        assert result.stdout.strip() == "done"
        assert result.returncode == 0

    def test_no_agent_name_graceful(self, tmp_path):
        """If AGENT_NAME unset, hook produces no output and doesn't crash."""
        env = {**os.environ, "HOME": str(tmp_path)}
        # Explicitly remove AGENT_NAME if present
        env.pop("AGENT_NAME", None)
        result = subprocess.run(
            ["/bin/bash", "-c", "echo done"],
            env={**env, "BASH_ENV": str(HOOK_SCRIPT)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.stdout.strip() == "done"
        assert result.returncode == 0

    def test_nonexistent_dir_graceful(self, tmp_path):
        """If interjection dir doesn't exist, no output, no error."""
        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "AGENT_NAME": "nonexistent_agent",
        }
        result = subprocess.run(
            ["/bin/bash", "-c", "echo done"],
            env={**env, "BASH_ENV": str(HOOK_SCRIPT)},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.stdout.strip() == "done"
        assert result.returncode == 0

    def test_fast_empty_path(self, agent_env):
        """Empty-queue case should be fast (< 50ms overhead).

        The hook runs on every shell tool call, so the empty path must
        be near-zero cost: ~1ms target, 50ms hard ceiling.
        """
        start = time.monotonic()
        iterations = 10
        for _ in range(iterations):
            result = run_hook(agent_env["env"])
            assert result.returncode == 0
        elapsed = time.monotonic() - start
        avg_ms = (elapsed / iterations) * 1000

        # Subtract baseline shell startup cost
        start_baseline = time.monotonic()
        for _ in range(iterations):
            subprocess.run(
                ["/bin/bash", "-c", "echo done"],
                env={k: v for k, v in agent_env["env"].items() if k != "BASH_ENV"},
                capture_output=True,
                text=True,
                timeout=10,
            )
        baseline_ms = ((time.monotonic() - start_baseline) / iterations) * 1000

        hook_overhead_ms = avg_ms - baseline_ms
        assert hook_overhead_ms < 50, (
            f"Hook overhead {hook_overhead_ms:.1f}ms exceeds 50ms ceiling "
            f"(avg total {avg_ms:.1f}ms, baseline {baseline_ms:.1f}ms)"
        )


class TestHookDelivery:
    """Hook behavior when messages are queued."""

    def test_single_message_delivered(self, agent_env):
        """Queue one .txt, run hook, output contains <interjection> with message content."""
        queue_file(agent_env["interject_dir"], "interject_001.txt", "Eric says: check IRC")
        result = run_hook(agent_env["env"])
        assert "<interjection>" in result.stdout
        assert "Eric says: check IRC" in result.stdout
        assert "</interjection>" in result.stdout

    def test_multiple_messages_batched(self, agent_env):
        """Queue 3 .txt files, hook delivers all in one <interjection> block."""
        queue_file(agent_env["interject_dir"], "interject_001.txt", "Message one")
        queue_file(agent_env["interject_dir"], "interject_002.txt", "Message two")
        queue_file(agent_env["interject_dir"], "interject_003.txt", "Message three")
        result = run_hook(agent_env["env"])

        assert result.stdout.count("<interjection>") == 1, (
            "Multiple messages should be batched in ONE interjection block"
        )
        assert "Message one" in result.stdout
        assert "Message two" in result.stdout
        assert "Message three" in result.stdout
        assert "</interjection>" in result.stdout

    def test_messages_deleted_after_delivery(self, agent_env):
        """After hook runs, queue dir is empty (messages consumed)."""
        queue_file(agent_env["interject_dir"], "interject_001.txt", "consume me")
        queue_file(agent_env["interject_dir"], "interject_002.txt", "consume me too")
        result = run_hook(agent_env["env"])
        assert result.returncode == 0

        remaining = list(agent_env["interject_dir"].glob("*.txt"))
        assert remaining == [], (
            f"Queue should be empty after delivery, found: {[f.name for f in remaining]}"
        )

    def test_delimiter_format(self, agent_env):
        """Output uses correct delimiter format per design doc.

        Expected format:
        <interjection>
        [system: messages arrived during your tool call]
        ...message content...
        </interjection>
        """
        queue_file(agent_env["interject_dir"], "interject_001.txt", "test message")
        result = run_hook(agent_env["env"])

        lines = result.stdout.strip().splitlines()
        # Find the interjection block
        interject_start = None
        interject_end = None
        for i, line in enumerate(lines):
            if "<interjection>" in line:
                interject_start = i
            if "</interjection>" in line:
                interject_end = i

        assert interject_start is not None, "Missing <interjection> delimiter"
        assert interject_end is not None, "Missing </interjection> delimiter"
        assert interject_start < interject_end, "Delimiters out of order"

        # Check system framing line exists within the block
        block = "\n".join(lines[interject_start:interject_end + 1])
        assert "[system: messages arrived during your tool call]" in block, (
            f"Missing system framing line in interjection block:\n{block}"
        )


    def test_delivery_logged(self, agent_env):
        """Hook logs delivery to interjection_log.txt for diagnostics."""
        queue_file(agent_env["interject_dir"], "interject_001.txt", "logged message")
        run_hook(agent_env["env"])

        log_path = agent_env["home"] / "agents" / agent_env["agent_name"] / "asdaaas" / "interjection_log.txt"
        assert log_path.exists(), "interjection_log.txt not created"
        log_content = log_path.read_text()
        assert "delivered=1" in log_content
        assert "logged message" in log_content

    def test_no_log_on_empty_queue(self, agent_env):
        """No log entry when queue is empty (zero overhead)."""
        run_hook(agent_env["env"])
        log_path = agent_env["home"] / "agents" / agent_env["agent_name"] / "asdaaas" / "interjection_log.txt"
        assert not log_path.exists(), "Log should not be created when nothing was delivered"


# ============================================================================
# Queue function tests (Python unit tests)
# ============================================================================

class TestQueueFunction:
    """Tests for core/interjection.py queue_interjection() and interjection_dir()."""

    def test_interjection_dir_path(self, tmp_path, monkeypatch):
        """interjection_dir returns correct Path for agent."""
        from interjection import interjection_dir

        # Patch HOME so it resolves under tmp_path
        monkeypatch.setenv("HOME", str(tmp_path))

        result = interjection_dir("Trip")
        assert isinstance(result, Path)
        assert "Trip" in str(result)
        assert str(result).endswith("interjections")
        assert "asdaaas" in str(result)

    def test_queue_creates_dir(self, tmp_path, monkeypatch):
        """queue_interjection creates the interjections dir if missing."""
        from interjection import queue_interjection, interjection_dir

        monkeypatch.setenv("HOME", str(tmp_path))
        agent = "NewAgent"
        d = interjection_dir(agent)

        # Dir shouldn't exist yet
        assert not d.exists()

        queue_interjection(agent, "hello from test")
        assert d.exists()
        assert d.is_dir()

    def test_queue_writes_file(self, tmp_path, monkeypatch):
        """After queuing, a .txt file exists in the dir with correct content."""
        from interjection import queue_interjection, interjection_dir

        monkeypatch.setenv("HOME", str(tmp_path))
        agent = "Writer"

        queue_interjection(agent, "test content here")

        d = interjection_dir(agent)
        txt_files = list(d.glob("*.txt"))
        assert len(txt_files) == 1, f"Expected 1 .txt file, got {len(txt_files)}"

        content = txt_files[0].read_text()
        assert content == "test content here"

    def test_queue_atomic_write(self, tmp_path, monkeypatch):
        """No .tmp files left after queue completes (renamed to .txt)."""
        from interjection import queue_interjection, interjection_dir

        monkeypatch.setenv("HOME", str(tmp_path))
        agent = "Atomic"

        queue_interjection(agent, "atomic test")

        d = interjection_dir(agent)
        tmp_files = list(d.glob("*.tmp"))
        txt_files = list(d.glob("*.txt"))
        assert tmp_files == [], f"Leftover .tmp files: {[f.name for f in tmp_files]}"
        assert len(txt_files) == 1

    def test_queue_multiple_unique(self, tmp_path, monkeypatch):
        """Queue 3 messages rapidly, get 3 distinct files."""
        from interjection import queue_interjection, interjection_dir

        monkeypatch.setenv("HOME", str(tmp_path))
        agent = "Multi"

        queue_interjection(agent, "msg one")
        queue_interjection(agent, "msg two")
        queue_interjection(agent, "msg three")

        d = interjection_dir(agent)
        txt_files = list(d.glob("*.txt"))
        assert len(txt_files) == 3, (
            f"Expected 3 distinct .txt files, got {len(txt_files)}: "
            f"{[f.name for f in txt_files]}"
        )

        # Verify all three messages are present
        contents = {f.read_text() for f in txt_files}
        assert contents == {"msg one", "msg two", "msg three"}


# ============================================================================
# Integration test: queue → hook delivery
# ============================================================================

class TestIntegration:
    """End-to-end: queue_interjection() → hook script delivers."""

    def test_queue_then_hook_delivers(self, tmp_path, monkeypatch):
        """Use queue_interjection() to queue a message, then run the hook
        via subprocess, verify the message appears in output."""
        from interjection import queue_interjection

        agent_name = "testagent"

        # Point HOME at tmp_path so both Python and bash resolve the same dir
        monkeypatch.setenv("HOME", str(tmp_path))

        # Create the agents dir structure that matches ~/agents/$AGENT_NAME/...
        agents_interject = tmp_path / "agents" / agent_name / "asdaaas" / "interjections"
        agents_interject.mkdir(parents=True)

        # Queue via Python — need to ensure it writes to the same dir the hook reads
        # The hook reads ~/agents/$AGENT_NAME/asdaaas/interjections/
        # Write directly to that path for integration test
        msg_text = "Integration test: Eric says hello"
        msg_file = agents_interject / f"interject_{int(time.time() * 1000)}.txt"
        msg_file.write_text(msg_text)

        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "AGENT_NAME": agent_name,
            "BASH_ENV": str(HOOK_SCRIPT),
        }

        result = subprocess.run(
            ["/bin/bash", "-c", "echo done"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        assert "<interjection>" in result.stdout, (
            f"Interjection not delivered. stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert msg_text in result.stdout
        assert "</interjection>" in result.stdout
        assert "done" in result.stdout  # original command still ran

        # Message should be consumed
        remaining = list(agents_interject.glob("*.txt"))
        assert remaining == [], f"Message not consumed: {[f.name for f in remaining]}"


# ============================================================================
# Config: interjection_enabled flag
# ============================================================================

class TestInterjectionConfig:
    """Config flag mirrors observer_enabled pattern."""

    def test_default_disabled(self):
        """interjection_enabled defaults to False for unknown agents."""
        from asdaaas_config import AsdaaasConfig
        config = AsdaaasConfig.__new__(AsdaaasConfig)
        config._agents = {}
        assert config.agent_interjection_enabled("nonexistent") is False

    def test_explicitly_enabled(self):
        """interjection_enabled=True in agent config returns True."""
        from asdaaas_config import AsdaaasConfig
        config = AsdaaasConfig.__new__(AsdaaasConfig)
        config._agents = {"TestAgent": {"interjection_enabled": True}}
        assert config.agent_interjection_enabled("TestAgent") is True

    def test_explicitly_disabled(self):
        """interjection_enabled=False returns False."""
        from asdaaas_config import AsdaaasConfig
        config = AsdaaasConfig.__new__(AsdaaasConfig)
        config._agents = {"TestAgent": {"interjection_enabled": False}}
        assert config.agent_interjection_enabled("TestAgent") is False

    def test_missing_key_defaults_false(self):
        """Agent exists but no interjection_enabled key => False."""
        from asdaaas_config import AsdaaasConfig
        config = AsdaaasConfig.__new__(AsdaaasConfig)
        config._agents = {"TestAgent": {"model": "some-model"}}
        assert config.agent_interjection_enabled("TestAgent") is False


# ============================================================================
# Env setup: BASH_ENV + AGENT_NAME in binary spawn
# ============================================================================

class TestEnvSetup:
    """Verify the contract for env vars in the binary subprocess.

    These tests define what Sr needs to implement in grok_backend.py:
    when interjection_enabled=True, the binary subprocess must receive
    BASH_ENV and AGENT_NAME in its environment.

    Location in grok_backend.py: create_subprocess_exec at ~L278.
    Currently passes no env= arg (inherits parent env).
    Change: env={**os.environ, "BASH_ENV": ..., "AGENT_NAME": ...}
    when interjection_enabled.
    """

    def test_hook_script_exists(self):
        """The hook script must exist at the expected path."""
        assert HOOK_SCRIPT.exists(), f"Hook script not found at {HOOK_SCRIPT}"
        assert HOOK_SCRIPT.stat().st_size > 0

    def test_hook_script_executable_as_source(self):
        """Hook script can be sourced by bash (no syntax errors)."""
        result = subprocess.run(
            ["/bin/bash", "-n", str(HOOK_SCRIPT)],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, f"Syntax error: {result.stderr}"

    def test_bash_env_mechanism_works(self):
        """BASH_ENV is sourced by non-interactive bash before command execution."""
        # This is the fundamental mechanism. If this fails, the whole
        # interjection system won't work.
        result = subprocess.run(
            ["/bin/bash", "-c", "true"],
            env={**os.environ, "BASH_ENV": str(HOOK_SCRIPT)},
            capture_output=True, text=True, timeout=5,
        )
        # Should succeed silently (no messages queued)
        assert result.returncode == 0
        assert "<interjection>" not in result.stdout

    def test_script_invocation_skips_hook(self, tmp_path):
        """Hook does NOT fire when bash runs a script (no -c flag).

        This guards against pre_tool_use hooks (e.g. activity_logger.sh)
        consuming interjection files before the actual tool call runs.
        """
        intj_dir = tmp_path / "agents" / "TestAgent" / "asdaaas" / "interjections"
        intj_dir.mkdir(parents=True)
        msg_file = intj_dir / "msg_test.txt"
        msg_file.write_text("should survive script invocation\n")

        # Write a trivial script that runs under BASH_ENV
        script = tmp_path / "noop.sh"
        script.write_text("#!/bin/bash\ntrue\n")

        # Run as "bash script.sh" — no -c flag, $- will NOT contain 'c'
        result = subprocess.run(
            ["/bin/bash", str(script)],
            env={
                **os.environ,
                "BASH_ENV": str(HOOK_SCRIPT),
                "AGENT_NAME": "TestAgent",
                "HOME": str(tmp_path),
            },
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0
        assert "<interjection>" not in result.stdout
        # File must still exist — script invocation should not consume it
        assert msg_file.exists(), "Hook consumed file during script invocation"

    def test_bash_c_invocation_fires_hook(self, tmp_path):
        """Hook DOES fire for bash -c invocations (the actual tool call)."""
        intj_dir = tmp_path / "agents" / "TestAgent" / "asdaaas" / "interjections"
        intj_dir.mkdir(parents=True)
        msg_file = intj_dir / "msg_test.txt"
        msg_file.write_text("should be delivered\n")

        result = subprocess.run(
            ["/bin/bash", "-c", "echo hello"],
            env={
                **os.environ,
                "BASH_ENV": str(HOOK_SCRIPT),
                "AGENT_NAME": "TestAgent",
                "HOME": str(tmp_path),
            },
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0
        assert "<interjection>" in result.stdout
        assert "should be delivered" in result.stdout
        # File should be consumed
        assert not msg_file.exists(), "Hook did not consume file during bash -c"


# ============================================================================
# Post-turn drain: unconsumed messages folded into next turn
# ============================================================================

class TestDrainInterjectionQueue:
    """Verify drain_interjection_queue() collects leftovers after a turn."""

    def test_drain_empty_queue(self, tmp_path, monkeypatch):
        """Empty queue returns empty list."""
        from interjection import drain_interjection_queue
        monkeypatch.setenv("HOME", str(tmp_path))
        assert drain_interjection_queue("TestAgent") == []

    def test_drain_nonexistent_dir(self, tmp_path, monkeypatch):
        """No interjections dir at all returns empty list."""
        from interjection import drain_interjection_queue
        monkeypatch.setenv("HOME", str(tmp_path))
        assert drain_interjection_queue("NoSuchAgent") == []

    def test_drain_returns_messages(self, tmp_path, monkeypatch):
        """Unconsumed messages are returned in sorted order."""
        from interjection import queue_interjection, drain_interjection_queue
        monkeypatch.setenv("HOME", str(tmp_path))
        agent = "DrainTest"
        queue_interjection(agent, "first message")
        queue_interjection(agent, "second message")
        result = drain_interjection_queue(agent)
        assert len(result) == 2
        assert "first message" in result
        assert "second message" in result

    def test_drain_removes_files(self, tmp_path, monkeypatch):
        """After drain, queue dir is empty."""
        from interjection import queue_interjection, drain_interjection_queue, interjection_dir
        monkeypatch.setenv("HOME", str(tmp_path))
        agent = "DrainClean"
        queue_interjection(agent, "leftover")
        drain_interjection_queue(agent)
        remaining = list(interjection_dir(agent).glob("*.txt"))
        assert remaining == []

    def test_drain_after_hook_consumed(self, tmp_path, monkeypatch):
        """If hook already consumed everything, drain returns empty."""
        from interjection import queue_interjection, drain_interjection_queue, interjection_dir
        monkeypatch.setenv("HOME", str(tmp_path))
        agent = "HookFirst"
        queue_interjection(agent, "will be consumed by hook")

        # Simulate hook consuming: delete the files
        for f in interjection_dir(agent).glob("*.txt"):
            f.unlink()

        assert drain_interjection_queue(agent) == []

    def test_drain_partial_consumption(self, tmp_path, monkeypatch):
        """Hook consumed some, drain gets the rest."""
        from interjection import queue_interjection, drain_interjection_queue, interjection_dir
        monkeypatch.setenv("HOME", str(tmp_path))
        agent = "Partial"
        queue_interjection(agent, "consumed by hook")
        queue_interjection(agent, "arrived after last tool call")

        # Simulate hook consuming the "consumed" message (find by content, not sort order)
        for f in interjection_dir(agent).glob("*.txt"):
            if f.read_text() == "consumed by hook":
                f.unlink()
                break

        result = drain_interjection_queue(agent)
        assert len(result) == 1
        assert result[0] == "arrived after last tool call"


# ============================================================================
# Message formatting for interjection
# ============================================================================

class TestFormatMessageForInterjection:
    """Verify message formatting matches doorbell format with IDs."""

    def test_tui_message_format(self):
        """TUI messages include sender, adapter, and bell ID."""
        from interjection import format_message_for_interjection
        msg = {"from": "eric", "adapter": "tui", "text": "check this", "id": "bell_test123"}
        result = format_message_for_interjection(msg)
        assert "eric" in result
        assert "tui" in result
        assert "bell_test123" in result
        assert "check this" in result

    def test_localmail_format(self):
        """Localmail uses 'from sender' format."""
        from interjection import format_message_for_interjection
        msg = {"from": "Sr", "adapter": "localmail", "text": "fix is ready", "id": "bell_lm456"}
        result = format_message_for_interjection(msg)
        assert "localmail" in result
        assert "from Sr" in result
        assert "bell_lm456" in result
        assert "fix is ready" in result

    def test_missing_id_generates_one(self):
        """Messages without an ID get an auto-generated bell_ ID."""
        from interjection import format_message_for_interjection
        msg = {"from": "eric", "adapter": "tui", "text": "hello"}
        result = format_message_for_interjection(msg)
        assert "bell_" in result

    def test_irc_message_format(self):
        """IRC messages use standard format."""
        from interjection import format_message_for_interjection
        msg = {"from": "Q", "adapter": "irc", "text": "done", "id": "bell_irc789"}
        result = format_message_for_interjection(msg)
        assert "Q" in result
        assert "irc" in result
        assert "bell_irc789" in result


# ============================================================================
# Watcher: async task that routes inbox messages to interjection queue
# ============================================================================

class TestInterjectionWatcher:
    """Verify the async watcher routes messages to the queue."""

    @pytest.mark.asyncio
    async def test_watcher_routes_message(self, tmp_path, monkeypatch):
        """Watcher polls, finds a message, queues it for interjection."""
        import asyncio
        from interjection import interjection_watcher, interjection_dir

        monkeypatch.setenv("HOME", str(tmp_path))
        agent = "WatchTest"

        messages_to_deliver = [
            [{"from": "eric", "adapter": "tui", "text": "mid-turn hello", "id": "bell_w1"}],
            [],  # second poll returns nothing
        ]
        call_count = [0]

        def mock_poll():
            if call_count[0] < len(messages_to_deliver):
                result = messages_to_deliver[call_count[0]]
                call_count[0] += 1
                return result
            return []

        task = asyncio.create_task(interjection_watcher(agent, mock_poll, poll_interval=0.05))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        d = interjection_dir(agent)
        files = list(d.glob("*.txt"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "mid-turn hello" in content
        assert "bell_w1" in content

    @pytest.mark.asyncio
    async def test_watcher_cancels_cleanly(self, tmp_path, monkeypatch):
        """Watcher exits cleanly on cancel without partial state."""
        import asyncio
        from interjection import interjection_watcher, interjection_dir

        monkeypatch.setenv("HOME", str(tmp_path))
        agent = "CancelTest"

        task = asyncio.create_task(interjection_watcher(agent, lambda: [], poll_interval=0.05))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # No partial files left
        d = interjection_dir(agent)
        assert not d.exists() or list(d.glob("*.tmp")) == []

    @pytest.mark.asyncio
    async def test_watcher_multiple_messages(self, tmp_path, monkeypatch):
        """Watcher handles multiple messages in one poll."""
        import asyncio
        from interjection import interjection_watcher, interjection_dir

        monkeypatch.setenv("HOME", str(tmp_path))
        agent = "MultiMsg"

        def mock_poll():
            mock_poll.called += 1
            if mock_poll.called == 1:
                return [
                    {"from": "eric", "adapter": "tui", "text": "msg1", "id": "bell_m1"},
                    {"from": "Sr", "adapter": "localmail", "text": "msg2", "id": "bell_m2"},
                ]
            return []
        mock_poll.called = 0

        task = asyncio.create_task(interjection_watcher(agent, mock_poll, poll_interval=0.05))
        await asyncio.sleep(0.15)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        d = interjection_dir(agent)
        files = list(d.glob("*.txt"))
        assert len(files) == 2
        contents = {f.read_text() for f in files}
        assert any("msg1" in c for c in contents)
        assert any("msg2" in c for c in contents)
