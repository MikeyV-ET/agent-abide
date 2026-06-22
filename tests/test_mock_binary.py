"""
test_mock_binary.py -- Unit tests for MockBinary scenario execution.

Tests that MockBinary correctly implements AgentBackend and produces
the right ResponseResults and updates.jsonl events for each step type.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

import pytest
from mock_binary import (
    MockBinary, NormalResponse, ToolCallOnly, DoomLoop,
    Compaction, EmptyResponse, SlowResponse,
)
from agent_backend import ResponseResult, TurnCancelled


@pytest.fixture
def tmp_sessions(tmp_path, monkeypatch):
    """Redirect session storage to tmp_path."""
    sessions_dir = tmp_path / ".grok" / "sessions"
    sessions_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_normal_response(tmp_sessions):
    mock = MockBinary([NormalResponse(speech="Hello.", tokens=5000)])
    await mock.start(str(tmp_sessions / "agent"))

    handle = await mock.send_prompt("Hi")
    result = await mock.collect_response(handle)

    assert result.speech == "Hello."
    assert result.total_tokens == 5000
    assert result.stop_reason == "completed"
    assert mock.steps_remaining == 0


@pytest.mark.asyncio
async def test_tool_call_only_empty_speech(tmp_sessions):
    mock = MockBinary([ToolCallOnly(retry_duration=0.1, resolve_speech="")])
    await mock.start(str(tmp_sessions / "agent"))

    handle = await mock.send_prompt("Do something")
    result = await mock.collect_response(handle)

    assert result.speech == ""
    assert result.stop_reason == "completed"


@pytest.mark.asyncio
async def test_tool_call_only_resolved(tmp_sessions):
    mock = MockBinary([ToolCallOnly(retry_duration=0.1, resolve_speech="Done.")])
    await mock.start(str(tmp_sessions / "agent"))

    handle = await mock.send_prompt("Work")
    result = await mock.collect_response(handle)

    assert result.speech == "Done."


@pytest.mark.asyncio
async def test_doom_loop(tmp_sessions):
    mock = MockBinary([DoomLoop(exit_count=5)])
    await mock.start(str(tmp_sessions / "agent"))

    handle = await mock.send_prompt("Crash")
    result = await mock.collect_response(handle)

    assert result.speech == ""
    assert result.stop_reason == "doom_loop"


@pytest.mark.asyncio
async def test_compaction(tmp_sessions):
    mock = MockBinary([Compaction(tokens_before=150000, tokens_after=30000)])
    await mock.start(str(tmp_sessions / "agent"))

    handle = await mock.send_prompt("Compact")
    result = await mock.collect_response(handle)

    assert result.total_tokens == 30000

    has_event, after, before = mock.pop_compaction_event()
    assert has_event is True
    assert after == 30000
    assert before == 150000

    # Second pop should be empty
    has_event2, _, _ = mock.pop_compaction_event()
    assert has_event2 is False


@pytest.mark.asyncio
async def test_empty_response(tmp_sessions):
    mock = MockBinary([EmptyResponse(tokens=3000)])
    await mock.start(str(tmp_sessions / "agent"))

    handle = await mock.send_prompt("Nothing")
    result = await mock.collect_response(handle)

    assert result.speech == ""
    assert result.total_tokens == 3000


@pytest.mark.asyncio
async def test_slow_response(tmp_sessions):
    mock = MockBinary([SlowResponse(speech="Finally.", delay=0.2, tokens=7000)])
    await mock.start(str(tmp_sessions / "agent"))

    handle = await mock.send_prompt("Wait")
    result = await mock.collect_response(handle)

    assert result.speech == "Finally."
    assert result.total_tokens == 7000


@pytest.mark.asyncio
async def test_multi_step_scenario(tmp_sessions):
    scenario = [
        NormalResponse(speech="Step 1.", tokens=5000),
        ToolCallOnly(retry_duration=0.1, resolve_speech="Step 2.", tokens=8000),
        NormalResponse(speech="Step 3.", tokens=12000),
    ]
    mock = MockBinary(scenario)
    await mock.start(str(tmp_sessions / "agent"))

    # Step 1
    h1 = await mock.send_prompt("Go")
    r1 = await mock.collect_response(h1)
    assert r1.speech == "Step 1."
    assert mock.steps_remaining == 2

    # Step 2
    h2 = await mock.send_prompt("Continue")
    r2 = await mock.collect_response(h2)
    assert r2.speech == "Step 2."
    assert mock.steps_remaining == 1

    # Step 3
    h3 = await mock.send_prompt("More")
    r3 = await mock.collect_response(h3)
    assert r3.speech == "Step 3."
    assert mock.steps_remaining == 0


@pytest.mark.asyncio
async def test_updates_jsonl_written(tmp_sessions):
    mock = MockBinary([NormalResponse(speech="Test.", tokens=5000)])
    await mock.start(str(tmp_sessions / "agent"))

    handle = await mock.send_prompt("Hi")
    await mock.collect_response(handle)

    # Find the updates.jsonl
    encoded = str(tmp_sessions / "agent").replace("/", "%2F")
    updates_path = Path(tmp_sessions) / ".grok" / "sessions" / encoded / mock.session_id / "updates.jsonl"
    assert updates_path.exists()

    lines = updates_path.read_text().strip().split("\n")
    events = [json.loads(l) for l in lines]

    # Should have: user_message_chunk, agent_message_chunk (with _meta)
    su_types = [e.get("params", {}).get("update", {}).get("sessionUpdate", "") for e in events]
    assert "user_message_chunk" in su_types
    assert "agent_message_chunk" in su_types


@pytest.mark.asyncio
async def test_past_end_returns_empty(tmp_sessions):
    mock = MockBinary([NormalResponse(speech="Only one.", tokens=5000)])
    await mock.start(str(tmp_sessions / "agent"))

    h1 = await mock.send_prompt("Go")
    await mock.collect_response(h1)

    # Past the scenario
    h2 = await mock.send_prompt("More")
    r2 = await mock.collect_response(h2)
    assert r2.speech == ""


@pytest.mark.asyncio
async def test_cancel_event(tmp_sessions):
    mock = MockBinary([SlowResponse(speech="Slow.", delay=10.0, tokens=5000)])
    await mock.start(str(tmp_sessions / "agent"))

    cancel = asyncio.Event()
    cancel.set()  # Already cancelled

    handle = await mock.send_prompt("Go")
    with pytest.raises(TurnCancelled):
        await mock.collect_response(handle, cancel_event=cancel)


@pytest.mark.asyncio
async def test_properties(tmp_sessions):
    mock = MockBinary([NormalResponse(tokens=5000)], context_window=100000)
    await mock.start(str(tmp_sessions / "agent"))

    assert mock.proc is None
    assert mock.session_id is not None
    assert mock.model_id == "mock-model"
    assert mock.context_window == 100000
    assert mock.total_tokens == 0

    h = await mock.send_prompt("Go")
    await mock.collect_response(h)
    assert mock.total_tokens == 5000
    assert mock.prompt_count == 1
    assert mock.last_prompt == "Go"