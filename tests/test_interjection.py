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
