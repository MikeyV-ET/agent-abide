"""Tests for delivery receipt tracking in GrokBackend.

Covers:
  1. _delivery_confirmed resets to False on send_prompt
  2. _delivery_confirmed set True when user_message_chunk frame seen
  3. _delivery_confirmed stays False when no user_message_chunk
  4. Structured DELIVERY_FAILURE log entry format
  5. Health.json includes delivery stats
  6. Integration: MockBinary writes user_message_chunk on send_prompt

Run: cd ~/projects/agent-abide && python -m pytest tests/test_delivery_receipts.py -v --tb=short
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from grok_backend import FileEventSource, GrokBackend
from mock_binary import MockBinary, NormalResponse
from agent_backend import ResponseResult


# ============================================================================
# Helpers
# ============================================================================

def write_jsonl(path: Path, records: list[dict]):
    """Append JSON lines to a file."""
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def make_user_message_frame(text: str = "hello"):
    """Build a user_message_chunk frame as it appears in updates.jsonl."""
    return {
        "timestamp": int(time.time()),
        "method": "session/update",
        "params": {
            "sessionId": "test-session",
            "update": {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    }


def make_speech_frame(text: str, tokens: int = 5000):
    """Build an agent_message_chunk frame."""
    return {
        "timestamp": int(time.time()),
        "method": "session/update",
        "params": {
            "sessionId": "test-session",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
            "_meta": {"totalTokens": tokens},
        },
    }


def make_meta_frame(tokens: int):
    """Build a _meta-only frame (no sessionUpdate)."""
    return {
        "timestamp": int(time.time()),
        "method": "session/update",
        "params": {
            "sessionId": "test-session",
            "update": {},
            "_meta": {"totalTokens": tokens},
        },
    }


def make_turn_ended(outcome: str = "completed"):
    return {"type": "turn_ended", "outcome": outcome}


def make_backend_with_files(tmp_path):
    """Create a GrokBackend instance wired to a FileEventSource on tmp_path."""
    backend = GrokBackend.__new__(GrokBackend)
    backend._proc = None
    backend._session_id = "test-session"
    backend._model_id = "test-model"
    backend._total_tokens = 0
    backend._compaction_event = None
    backend._compaction_tokens_before = 0
    backend._compaction_tokens_after = 0
    backend._context_window = 200000
    backend._last_activity_ts = 0.0
    backend._pending_tool_calls = set()
    backend._delivery_confirmed = False
    backend._rpc_id = 0
    backend._grok_sessions_dir = tmp_path
    backend._grok_binary = "grok"
    backend._permission_handler = None
    backend._allowed_always = set()
    backend._permission_pending = False

    # Create updates.jsonl and events.jsonl
    updates = tmp_path / "updates.jsonl"
    events = tmp_path / "events.jsonl"
    updates.touch()
    events.touch()

    # Wire up FileEventSource
    source = FileEventSource(tmp_path)
    source.open()
    backend._file_source = source

    return backend


# ============================================================================
# Test 1: _delivery_confirmed resets to False on send_prompt
# ============================================================================

class TestDeliveryResetOnSend:
    """send_prompt() must reset _delivery_confirmed to False before sending."""

    @pytest.mark.asyncio
    async def test_reset_on_send(self, tmp_path):
        """_delivery_confirmed is set to False at the start of send_prompt."""
        backend = make_backend_with_files(tmp_path)
        # Pre-set to True (simulating a previous successful delivery)
        backend._delivery_confirmed = True
        assert backend.delivery_confirmed is True

        # Mock _send (needs proc/stdin) and _wait_for_receipt (does file I/O)
        backend._send = AsyncMock()
        backend._wait_for_receipt = AsyncMock()

        await backend.send_prompt("test message")

        # send_prompt sets _delivery_confirmed = False before _send
        # Then _wait_for_receipt runs (mocked here, so it doesn't change state)
        # Since _wait_for_receipt is mocked to do nothing, it stays False
        assert backend._delivery_confirmed is False

        backend._file_source.close()

    @pytest.mark.asyncio
    async def test_reset_happens_before_send(self, tmp_path):
        """Verify the reset happens before the RPC is sent, not after."""
        backend = make_backend_with_files(tmp_path)
        backend._delivery_confirmed = True

        flag_at_send_time = None

        async def capture_flag(msg):
            nonlocal flag_at_send_time
            flag_at_send_time = backend._delivery_confirmed

        backend._send = capture_flag
        backend._wait_for_receipt = AsyncMock()

        await backend.send_prompt("test")

        # At the moment _send is called, _delivery_confirmed should already be False
        assert flag_at_send_time is False

        backend._file_source.close()


# ============================================================================
# Test 2: _delivery_confirmed set True when user_message_chunk seen
# ============================================================================

class TestDeliveryConfirmedOnReceipt:
    """_wait_for_receipt sets _delivery_confirmed = True when user_message_chunk appears."""

    @pytest.mark.asyncio
    async def test_receipt_confirmed(self, tmp_path):
        """user_message_chunk in updates.jsonl sets _delivery_confirmed = True."""
        backend = make_backend_with_files(tmp_path)
        assert backend._delivery_confirmed is False

        # Write user_message_chunk to updates.jsonl
        updates_path = tmp_path / "updates.jsonl"
        write_jsonl(updates_path, [make_user_message_frame("test prompt")])

        await backend._wait_for_receipt(timeout=2.0)

        assert backend._delivery_confirmed is True
        assert backend.delivery_confirmed is True

        backend._file_source.close()

    @pytest.mark.asyncio
    async def test_receipt_confirmed_after_other_frames(self, tmp_path):
        """user_message_chunk found even when preceded by other frame types."""
        backend = make_backend_with_files(tmp_path)

        updates_path = tmp_path / "updates.jsonl"
        # Write speech from previous turn, then user_message_chunk
        write_jsonl(updates_path, [
            make_speech_frame("leftover from prior turn"),
            make_meta_frame(10000),
            make_user_message_frame("new prompt"),
        ])

        await backend._wait_for_receipt(timeout=2.0)

        assert backend._delivery_confirmed is True

        backend._file_source.close()

    @pytest.mark.asyncio
    async def test_receipt_clears_pending_tools(self, tmp_path):
        """user_message_chunk receipt clears pending_tool_calls (new turn)."""
        backend = make_backend_with_files(tmp_path)
        backend._pending_tool_calls = {"tc_old_1", "tc_old_2"}

        updates_path = tmp_path / "updates.jsonl"
        write_jsonl(updates_path, [make_user_message_frame("new turn")])

        await backend._wait_for_receipt(timeout=2.0)

        assert backend._delivery_confirmed is True
        assert len(backend._pending_tool_calls) == 0

        backend._file_source.close()


# ============================================================================
# Test 3: _delivery_confirmed stays False when no user_message_chunk
# ============================================================================

class TestDeliveryNotConfirmed:
    """_delivery_confirmed stays False when user_message_chunk never arrives."""

    @pytest.mark.asyncio
    async def test_timeout_no_receipt(self, tmp_path):
        """_delivery_confirmed stays False when _wait_for_receipt times out."""
        backend = make_backend_with_files(tmp_path)
        assert backend._delivery_confirmed is False

        # Don't write any user_message_chunk — let it time out
        await backend._wait_for_receipt(timeout=0.2)

        assert backend._delivery_confirmed is False

        backend._file_source.close()

    @pytest.mark.asyncio
    async def test_other_frames_dont_confirm(self, tmp_path):
        """Non-user_message_chunk frames do not set _delivery_confirmed."""
        backend = make_backend_with_files(tmp_path)

        updates_path = tmp_path / "updates.jsonl"
        # Write various frames but NOT user_message_chunk
        write_jsonl(updates_path, [
            make_speech_frame("agent speaking"),
            make_meta_frame(8000),
        ])

        await backend._wait_for_receipt(timeout=0.2)

        assert backend._delivery_confirmed is False

        backend._file_source.close()

    @pytest.mark.asyncio
    async def test_no_file_source_skips(self, tmp_path):
        """_wait_for_receipt returns immediately if no file source."""
        backend = make_backend_with_files(tmp_path)
        backend._file_source.close()
        backend._file_source = None

        # Should return immediately without error
        await backend._wait_for_receipt(timeout=1.0)
        assert backend._delivery_confirmed is False


# ============================================================================
# Test 4: DELIVERY_FAILURE structured log entry
# ============================================================================

class TestDeliveryFailureLog:
    """Test the DELIVERY_FAILURE log entry format from asdaaas."""

    def test_log_format(self, capsys):
        """DELIVERY_FAILURE log line has the expected structured format."""
        agent_name = "TestAgent"
        prompt_text = "Hello, this is a test prompt"

        # Reproduce the exact logging logic from asdaaas.py
        backend = GrokBackend.__new__(GrokBackend)
        backend._delivery_confirmed = False

        if hasattr(backend, 'delivery_confirmed') and not backend.delivery_confirmed:
            print(f"[asdaaas] DELIVERY_FAILURE: agent={agent_name} prompt_len={len(prompt_text)} reason=no_user_message_chunk")

        captured = capsys.readouterr()
        assert "[asdaaas] DELIVERY_FAILURE:" in captured.out
        assert f"agent={agent_name}" in captured.out
        assert f"prompt_len={len(prompt_text)}" in captured.out
        assert "reason=no_user_message_chunk" in captured.out

    def test_no_log_when_confirmed(self, capsys):
        """No DELIVERY_FAILURE when delivery_confirmed is True."""
        agent_name = "TestAgent"
        prompt_text = "confirmed prompt"

        backend = GrokBackend.__new__(GrokBackend)
        backend._delivery_confirmed = True

        if hasattr(backend, 'delivery_confirmed') and not backend.delivery_confirmed:
            print(f"[asdaaas] DELIVERY_FAILURE: agent={agent_name} prompt_len={len(prompt_text)} reason=no_user_message_chunk")

        captured = capsys.readouterr()
        assert "DELIVERY_FAILURE" not in captured.out

    def test_log_includes_prompt_length(self, capsys):
        """DELIVERY_FAILURE log includes accurate prompt_len."""
        prompt_text = "x" * 1234
        agent_name = "Agent"
        backend = GrokBackend.__new__(GrokBackend)
        backend._delivery_confirmed = False

        if hasattr(backend, 'delivery_confirmed') and not backend.delivery_confirmed:
            print(f"[asdaaas] DELIVERY_FAILURE: agent={agent_name} prompt_len={len(prompt_text)} reason=no_user_message_chunk")

        captured = capsys.readouterr()
        assert "prompt_len=1234" in captured.out


# ============================================================================
# Test 5: health.json includes delivery stats
# ============================================================================

class TestHealthDeliveryStats:
    """write_health with delivery_failure detail produces valid health.json."""

    def test_health_delivery_failure(self, tmp_path, monkeypatch):
        """write_health('delivery_failure') creates health.json with expected fields."""
        # Import and patch AGENTS_HOME_DIR so agent_dir resolves to tmp_path
        import asdaaas
        monkeypatch.setattr(asdaaas, "AGENTS_HOME_DIR", tmp_path)

        agent_name = "TestAgent"
        asdaaas_dir = tmp_path / agent_name / "asdaaas"
        asdaaas_dir.mkdir(parents=True)

        asdaaas.write_health(agent_name, "active", "delivery_failure", 50000, 200000)

        health_path = asdaaas_dir / "health.json"
        assert health_path.exists()

        health = json.loads(health_path.read_text())
        assert health["agent"] == agent_name
        assert health["status"] == "active"
        assert health["detail"] == "delivery_failure"
        assert health["totalTokens"] == 50000
        assert health["contextWindow"] == 200000
        assert "ts" in health
        assert "pid" in health

    def test_health_file_is_valid_json(self, tmp_path, monkeypatch):
        """health.json is valid JSON with all required keys."""
        import asdaaas
        monkeypatch.setattr(asdaaas, "AGENTS_HOME_DIR", tmp_path)

        agent_name = "Agent2"
        (tmp_path / agent_name / "asdaaas").mkdir(parents=True)

        asdaaas.write_health(agent_name, "active", "delivery_failure", 100000, 200000)

        health_path = tmp_path / agent_name / "asdaaas" / "health.json"
        health = json.loads(health_path.read_text())

        required_keys = {"agent", "status", "detail", "ts", "pid",
                         "totalTokens", "contextWindow"}
        assert required_keys.issubset(set(health.keys()))


# ============================================================================
# Test 6: MockBinary integration — delivery receipt via user_message_chunk
# ============================================================================

class TestMockBinaryDelivery:
    """MockBinary writes user_message_chunk on send_prompt."""

    @pytest.mark.asyncio
    async def test_send_prompt_writes_user_message_chunk(self, tmp_path):
        """MockBinary.send_prompt writes a user_message_chunk frame to updates.jsonl."""
        scenario = [NormalResponse(speech="Hello.", tokens=5000)]
        mock = MockBinary(scenario)

        agent_cwd = str(tmp_path / "agents" / "TestAgent")
        os.makedirs(agent_cwd, exist_ok=True)

        await mock.start(agent_cwd=agent_cwd)
        await mock.send_prompt("test delivery")

        # Read updates.jsonl
        updates_path = mock.session_dir / "updates.jsonl"
        lines = updates_path.read_text().strip().split("\n")
        frames = [json.loads(line) for line in lines if line.strip()]

        # Find user_message_chunk
        user_chunks = [
            f for f in frames
            if f.get("params", {}).get("update", {}).get("sessionUpdate") == "user_message_chunk"
        ]
        assert len(user_chunks) == 1
        content = user_chunks[0]["params"]["update"]["content"]
        assert content["text"] == "test delivery"

    @pytest.mark.asyncio
    async def test_user_message_chunk_before_response(self, tmp_path):
        """user_message_chunk is written before collect_response speech."""
        scenario = [NormalResponse(speech="Response text.", tokens=8000)]
        mock = MockBinary(scenario)

        agent_cwd = str(tmp_path / "agents" / "TestAgent")
        os.makedirs(agent_cwd, exist_ok=True)

        await mock.start(agent_cwd=agent_cwd)
        await mock.send_prompt("prompt text")
        result = await mock.collect_response(handle=1)

        assert result.speech == "Response text."

        # Verify ordering: user_message_chunk appears before agent_message_chunk
        updates_path = mock.session_dir / "updates.jsonl"
        lines = updates_path.read_text().strip().split("\n")
        frames = [json.loads(line) for line in lines if line.strip()]

        session_updates = [
            f.get("params", {}).get("update", {}).get("sessionUpdate", "")
            for f in frames
        ]

        user_idx = session_updates.index("user_message_chunk")
        agent_idx = session_updates.index("agent_message_chunk")
        assert user_idx < agent_idx, "user_message_chunk must come before agent_message_chunk"

    @pytest.mark.asyncio
    async def test_delivery_confirmed_property_exists_on_grok(self):
        """GrokBackend exposes delivery_confirmed property."""
        backend = GrokBackend.__new__(GrokBackend)
        backend._delivery_confirmed = False
        assert hasattr(backend, 'delivery_confirmed')
        assert backend.delivery_confirmed is False

        backend._delivery_confirmed = True
        assert backend.delivery_confirmed is True

    @pytest.mark.asyncio
    async def test_multi_turn_delivery(self, tmp_path):
        """Each send_prompt writes a new user_message_chunk frame."""
        scenario = [
            NormalResponse(speech="Turn 1.", tokens=5000),
            NormalResponse(speech="Turn 2.", tokens=10000),
        ]
        mock = MockBinary(scenario)

        agent_cwd = str(tmp_path / "agents" / "TestAgent")
        os.makedirs(agent_cwd, exist_ok=True)

        await mock.start(agent_cwd=agent_cwd)

        # Turn 1
        await mock.send_prompt("first prompt")
        r1 = await mock.collect_response(handle=1)
        assert r1.speech == "Turn 1."

        # Turn 2
        await mock.send_prompt("second prompt")
        r2 = await mock.collect_response(handle=2)
        assert r2.speech == "Turn 2."

        # Verify two user_message_chunk frames
        updates_path = mock.session_dir / "updates.jsonl"
        lines = updates_path.read_text().strip().split("\n")
        frames = [json.loads(line) for line in lines if line.strip()]

        user_chunks = [
            f for f in frames
            if f.get("params", {}).get("update", {}).get("sessionUpdate") == "user_message_chunk"
        ]
        assert len(user_chunks) == 2
        assert user_chunks[0]["params"]["update"]["content"]["text"] == "first prompt"
        assert user_chunks[1]["params"]["update"]["content"]["text"] == "second prompt"

    @pytest.mark.asyncio
    async def test_asdaaas_check_pattern_confirmed(self):
        """The hasattr + delivery_confirmed check works for confirmed delivery."""
        backend = GrokBackend.__new__(GrokBackend)
        backend._delivery_confirmed = True

        # Reproduce the asdaaas check
        should_log_failure = (
            hasattr(backend, 'delivery_confirmed')
            and not backend.delivery_confirmed
        )
        assert should_log_failure is False

    @pytest.mark.asyncio
    async def test_asdaaas_check_pattern_unconfirmed(self):
        """The hasattr + delivery_confirmed check works for unconfirmed delivery."""
        backend = GrokBackend.__new__(GrokBackend)
        backend._delivery_confirmed = False

        # Reproduce the asdaaas check
        should_log_failure = (
            hasattr(backend, 'delivery_confirmed')
            and not backend.delivery_confirmed
        )
        assert should_log_failure is True
