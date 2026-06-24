"""Integration tests: agent <-> asdaaas interaction via MockAgent.

Tests the full loop using mock frames that match the real grok binary's
JSON-RPC protocol. No API calls, deterministic, fast.

Tests cover:
  - collect_response correctly assembles speech/thoughts/meta from frames
  - Speech chunking produces correct concatenated output
  - Thought chunks are separated from speech
  - Tool call callbacks fire correctly
  - totalTokens extracted from streaming _meta and final response
  - prompt_complete tightens keepalive for _meta capture
  - Context tag formatting with compaction status
  - Gaze injection into prompt text
  - Empty/minimal responses handled gracefully

Ported from mikeyv-infra/tests/test_integration.py to agent-abide.
Import paths updated: core/ for asdaaas, local mock_agent.py.
"""

import asyncio
import json
import sys
import os
import pytest
from pathlib import Path

# agent-abide uses core/ for core modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from mock_agent import (
    build_prompt_response_frames, MockAgentWriter, _chunkify,
    notification, response,
)
import asdaaas


# ============================================================================
# Helpers
# ============================================================================

def frames_to_reader(frames):
    """Convert a list of frame dicts to an asyncio.StreamReader with the data."""
    reader = asyncio.StreamReader()
    for frame in frames:
        reader.feed_data((json.dumps(frame) + "\n").encode("utf-8"))
    reader.feed_eof()
    return reader


# ============================================================================
# MockAgent unit tests
# ============================================================================

class TestMockAgentFrameGeneration:
    """Test that MockAgent produces correct frame sequences."""

    def test_speech_only(self):
        frames = build_prompt_response_frames(1, speech="Hello world")
        # Should have: speech chunks + prompt_complete + response
        speech_frames = [f for f in frames
                         if f.get("method") == "session/update"
                         and f.get("params", {}).get("update", {}).get("sessionUpdate") == "agent_message_chunk"]
        assert len(speech_frames) >= 1
        # Reassemble speech
        text = "".join(
            f["params"]["update"]["content"]["text"] for f in speech_frames
        )
        assert text == "Hello world"

    def test_thoughts_and_speech(self):
        frames = build_prompt_response_frames(1, speech="Yes", thoughts="Hmm let me think")
        thought_frames = [f for f in frames
                          if f.get("method") == "session/update"
                          and f.get("params", {}).get("update", {}).get("sessionUpdate") == "agent_thought_chunk"]
        speech_frames = [f for f in frames
                         if f.get("method") == "session/update"
                         and f.get("params", {}).get("update", {}).get("sessionUpdate") == "agent_message_chunk"]
        assert len(thought_frames) >= 1
        assert len(speech_frames) >= 1
        # Thoughts come before speech
        first_thought_idx = frames.index(thought_frames[0])
        first_speech_idx = frames.index(speech_frames[0])
        assert first_thought_idx < first_speech_idx

    def test_tool_calls(self):
        frames = build_prompt_response_frames(1, speech="done", tool_calls=["read_file", "run_terminal_cmd"])
        tool_frames = [f for f in frames
                       if f.get("method") == "session/update"
                       and f.get("params", {}).get("update", {}).get("sessionUpdate") == "tool_call"]
        assert len(tool_frames) == 2
        assert tool_frames[0]["params"]["update"]["title"] == "read_file"
        assert tool_frames[1]["params"]["update"]["title"] == "run_terminal_cmd"

    def test_prompt_complete_present(self):
        frames = build_prompt_response_frames(1, speech="ok")
        complete = [f for f in frames if f.get("method") == "_x.ai/session/prompt_complete"]
        assert len(complete) == 1

    def test_final_response_has_meta(self):
        frames = build_prompt_response_frames(42, speech="ok", total_tokens=75000)
        final = [f for f in frames if "id" in f and f["id"] == 42]
        assert len(final) == 1
        meta = final[0]["result"]["_meta"]
        assert meta["totalTokens"] == 75000
        assert meta["modelId"] == "mock-model"
        assert meta["stopReason"] == "end_turn"

    def test_streaming_meta_on_chunks(self):
        frames = build_prompt_response_frames(1, speech="Hello world", total_tokens=60000)
        for f in frames:
            if f.get("method") == "session/update":
                assert f["params"]["_meta"]["totalTokens"] == 60000

    def test_empty_speech(self):
        frames = build_prompt_response_frames(1, speech="")
        speech_frames = [f for f in frames
                         if f.get("method") == "session/update"
                         and f.get("params", {}).get("update", {}).get("sessionUpdate") == "agent_message_chunk"]
        assert len(speech_frames) == 0
        # Still has prompt_complete + response
        assert any(f.get("method") == "_x.ai/session/prompt_complete" for f in frames)
        assert any(f.get("id") == 1 for f in frames)

    def test_request_id_matches(self):
        for rid in [1, 42, 999]:
            frames = build_prompt_response_frames(rid, speech="ok")
            final = [f for f in frames if "id" in f]
            assert len(final) == 1
            assert final[0]["id"] == rid


class TestChunkify:
    def test_short_text(self):
        assert _chunkify("hi") == ["hi"]

    def test_splits_preserving_whitespace(self):
        chunks = _chunkify("Hello world this is a test", chunk_size=10)
        assert len(chunks) > 1
        # "".join must reconstruct original (matches collect_response behavior)
        assert "".join(chunks) == "Hello world this is a test"

    def test_empty_string(self):
        assert _chunkify("") == [""]


class TestMockAgentWriter:
    def test_collects_frames(self):
        writer = MockAgentWriter()
        writer.send_init_response(1)
        assert len(writer.frames) == 1
        assert writer.frames[0]["id"] == 1
        assert "protocolVersion" in writer.frames[0]["result"]

    def test_session_response(self):
        writer = MockAgentWriter()
        writer.send_session_response(2, session_id="test-session")
        assert writer.frames[0]["result"]["sessionId"] == "test-session"

    def test_prompt_response_sequence(self):
        writer = MockAgentWriter()
        writer.send_prompt_response(3, speech="Hello", total_tokens=80000)
        # Should have speech chunks + prompt_complete + response
        assert len(writer.frames) >= 3
        assert writer.frames[-1]["id"] == 3
        assert writer.frames[-1]["result"]["_meta"]["totalTokens"] == 80000


# ============================================================================
# Integration tests: collect_response with mock frames
# ============================================================================

class TestCollectResponseIntegration:
    """Test asdaaas.collect_response with MockAgent-generated frames."""

    @pytest.fixture(autouse=True)
    def reset_rpc_id(self):
        """Reset global RPC ID counter for deterministic test IDs."""
        asdaaas._rpc_id = 0

    @pytest.mark.asyncio
    async def test_speech_collected(self):
        """collect_response assembles speech chunks into full text."""
        frames = build_prompt_response_frames(1, speech="Hello world from mock agent")
        reader = frames_to_reader(frames)
        speech, thoughts, meta = await asdaaas.collect_response(reader, 1, timeout=5)
        assert speech == "Hello world from mock agent"
        assert thoughts == ""

    @pytest.mark.asyncio
    async def test_thoughts_collected(self):
        """collect_response separates thoughts from speech."""
        frames = build_prompt_response_frames(1, speech="Yes",
                                               thoughts="Let me think about this carefully")
        reader = frames_to_reader(frames)
        speech, thoughts, meta = await asdaaas.collect_response(reader, 1, timeout=5)
        assert speech == "Yes"
        assert thoughts == "Let me think about this carefully"

    @pytest.mark.asyncio
    async def test_total_tokens_from_meta(self):
        """collect_response extracts totalTokens from _meta."""
        frames = build_prompt_response_frames(1, speech="ok", total_tokens=95000)
        reader = frames_to_reader(frames)
        speech, thoughts, meta = await asdaaas.collect_response(reader, 1, timeout=5)
        assert meta["totalTokens"] == 95000

    @pytest.mark.asyncio
    async def test_model_id_and_stop_reason(self):
        """collect_response extracts modelId and stopReason from final response."""
        frames = build_prompt_response_frames(1, speech="ok", total_tokens=50000)
        reader = frames_to_reader(frames)
        _, _, meta = await asdaaas.collect_response(reader, 1, timeout=5)
        assert meta["modelId"] == "mock-model"
        assert meta["stopReason"] == "end_turn"

    @pytest.mark.asyncio
    async def test_tool_call_callback(self):
        """on_tool_call callback fires for each tool_call frame."""
        frames = build_prompt_response_frames(1, speech="done",
                                               tool_calls=["read_file", "grep"])
        reader = frames_to_reader(frames)
        tool_calls_seen = []
        speech, _, _ = await asdaaas.collect_response(
            reader, 1, timeout=5,
            on_tool_call=lambda title: tool_calls_seen.append(title))
        assert tool_calls_seen == ["read_file", "grep"]
        assert speech == "done"

    @pytest.mark.asyncio
    async def test_speech_chunk_callback(self):
        """on_speech_chunk callback fires for each speech chunk."""
        frames = build_prompt_response_frames(1, speech="Hello world from the mock")
        reader = frames_to_reader(frames)
        chunks_seen = []
        speech, _, _ = await asdaaas.collect_response(
            reader, 1, timeout=5,
            on_speech_chunk=lambda text: chunks_seen.append(text))
        assert len(chunks_seen) >= 1
        assert "".join(chunks_seen) == speech

    @pytest.mark.asyncio
    async def test_on_meta_callback(self):
        """on_meta callback fires with totalTokens from streaming _meta."""
        frames = build_prompt_response_frames(1, speech="ok", total_tokens=120000)
        reader = frames_to_reader(frames)
        meta_values = []
        await asdaaas.collect_response(
            reader, 1, timeout=5,
            on_meta=lambda tokens: meta_values.append(tokens))
        assert 120000 in meta_values

    @pytest.mark.asyncio
    async def test_empty_speech_response(self):
        """collect_response handles responses with no speech."""
        frames = build_prompt_response_frames(1, speech="", total_tokens=50000)
        reader = frames_to_reader(frames)
        speech, thoughts, meta = await asdaaas.collect_response(reader, 1, timeout=5)
        assert speech == ""
        assert meta["totalTokens"] == 50000

    @pytest.mark.asyncio
    async def test_multiple_sequential_responses(self):
        """Simulates multiple prompt/response cycles on the same stream."""
        all_frames = []
        all_frames.extend(build_prompt_response_frames(1, speech="First response", total_tokens=50000))
        all_frames.extend(build_prompt_response_frames(2, speech="Second response", total_tokens=55000))

        reader = frames_to_reader(all_frames)

        speech1, _, meta1 = await asdaaas.collect_response(reader, 1, timeout=5)
        assert speech1 == "First response"
        assert meta1["totalTokens"] == 50000

        speech2, _, meta2 = await asdaaas.collect_response(reader, 2, timeout=5)
        assert speech2 == "Second response"
        assert meta2["totalTokens"] == 55000


# ============================================================================
# Context tag + gaze integration
# ============================================================================

class TestContextTagIntegration:
    """Test context_left_tag formatting used in prompt injection."""

    def test_tag_with_compaction_available(self):
        tag = asdaaas.context_left_tag(100000, 200000, turns_since_compaction=5)
        assert "compaction available" in tag

    def test_tag_includes_gaze(self):
        gaze = {"speech": {"target": "irc", "params": {"room": "pm:eric"}}}
        tag = asdaaas.context_left_tag(100000, 200000, turns_since_compaction=5, gaze=gaze)
        assert "irc/pm:eric" in tag

    def test_tag_just_compacted(self):
        tag = asdaaas.context_left_tag(30000, 200000, turns_since_compaction=0)
        assert "just compacted" in tag

    def test_tag_tokens_remaining_correct(self):
        # 170k usable (85% of 200k), 100k used = 70k left
        tag = asdaaas.context_left_tag(100000, 200000, turns_since_compaction=5)
        assert "70k" in tag


# ============================================================================
# Prompt construction
# ============================================================================

class TestPromptConstruction:
    """Test how asdaaas builds prompts from messages + context tags."""

    def test_rpc_request_format(self):
        asdaaas._rpc_id = 0
        msg = asdaaas.rpc_request("session/prompt", {
            "sessionId": "test-session",
            "prompt": [{"type": "text", "text": "Hello"}],
        })
        parsed = json.loads(msg)
        assert parsed["jsonrpc"] == "2.0"
        assert parsed["method"] == "session/prompt"
        assert parsed["id"] == 1
        assert parsed["params"]["prompt"][0]["text"] == "Hello"

    def test_rpc_notification_no_id(self):
        msg = asdaaas.rpc_notification("notifications/initialized")
        parsed = json.loads(msg)
        assert "id" not in parsed
        assert parsed["method"] == "notifications/initialized"

    def test_rpc_id_increments(self):
        asdaaas._rpc_id = 0
        msg1 = json.loads(asdaaas.rpc_request("test1"))
        msg2 = json.loads(asdaaas.rpc_request("test2"))
        assert msg2["id"] == msg1["id"] + 1


# ============================================================================
# Doorbell formatting (what agent receives)
# ============================================================================

class TestDoorbellFormatIntegration:
    """Test doorbell formatting as seen by the agent."""

    def test_compact_confirm_doorbell_format(self):
        bell = {
            "adapter": "session",
            "command": "compact_confirm",
            "text": "Compaction requested. To confirm, create this file: touch /tmp/test.tmp",
            "id": "cpt_abc123",
            "delivered_count": 1,
        }
        formatted = asdaaas.format_doorbell(bell)
        assert "compact_confirm" in formatted
        assert "cpt_abc123" in formatted
        assert "touch /tmp/test.tmp" in formatted

    def test_irc_message_doorbell_format(self):
        bell = {
            "adapter": "irc",
            "text": "[IRC PM from eric]\nhello there",
            "id": "irc_msg_001",
            "delivered_count": 1,
        }
        formatted = asdaaas.format_doorbell(bell)
        assert "irc" in formatted
        assert "hello there" in formatted

    def test_heartbeat_doorbell_format(self):
        bell = {
            "adapter": "heartbeat",
            "command": "heartbeat",
            "text": "idle check",
            "id": "hb_001",
            "delivered_count": 1,
        }
        formatted = asdaaas.format_doorbell(bell)
        assert "heartbeat" in formatted
        assert "hb_001" in formatted


# ============================================================================
# BUG REPRODUCTION: reasoning gap causes speech loss
# ============================================================================
# Session 43 bug: model generates speech, then pauses > keepalive_timeout
# to think/plan, then generates more speech + tool calls + prompt_complete.
# collect_response exits during the pause, missing all subsequent frames.
#
# Evidence from production log:
#   - collect_response captured 1035 chars of speech, then exited
#   - drain found 152 stale frames: 8 tool_calls, 28 tool_call_updates,
#     104 agent_message_chunks (784 chars of speech), 1 prompt_complete
#   - The model was reasoning between speech chunks for > 30s
#
# This test reproduces the exact failure mode.

class TestReasoningGapSpeechLoss:
    """Reproduce: model pauses > keepalive between speech chunks, losing speech."""

    def test_reasoning_gap_loses_speech(self):
        """REPRODUCTION: A gap > keepalive_timeout between speech chunks
        causes collect_response to exit early, losing subsequent speech.

        Simulates the real scenario: model generates speech, pauses to
        reason (> keepalive_timeout), then generates more speech + tool
        calls + prompt_complete. The feeder runs concurrently with
        collect_response to accurately simulate the pipe timing.
        """
        async def _run():
            reader = asyncio.StreamReader()

            async def feed_frames():
                """Simulate grok binary writing frames with a reasoning gap."""
                # Phase 1: initial speech
                phase1 = [
                    notification("session/update", {
                        "update": {"sessionUpdate": "agent_message_chunk",
                                   "content": {"text": "Let me build "}},
                        "_meta": {"totalTokens": 50000}}),
                    notification("session/update", {
                        "update": {"sessionUpdate": "agent_message_chunk",
                                   "content": {"text": "the dashboard:"}},
                        "_meta": {"totalTokens": 50100}}),
                ]
                for f in phase1:
                    reader.feed_data((json.dumps(f) + "\n").encode())

                # Model pauses to think/plan — gap > keepalive_timeout
                await asyncio.sleep(0.7)

                # Phase 2: more speech + tool calls + prompt_complete
                phase2 = [
                    notification("session/update", {
                        "update": {"sessionUpdate": "agent_message_chunk",
                                   "content": {"text": " Done. "}},
                        "_meta": {"totalTokens": 60000}}),
                    notification("session/update", {
                        "update": {"sessionUpdate": "tool_call",
                                   "toolCallId": "tc1", "title": "run_terminal_cmd"},
                        "_meta": {"totalTokens": 60100}}),
                    notification("session/update", {
                        "update": {"sessionUpdate": "tool_call_update",
                                   "toolCallId": "tc1", "status": "completed"},
                        "_meta": {"totalTokens": 65000}}),
                    notification("session/update", {
                        "update": {"sessionUpdate": "agent_message_chunk",
                                   "content": {"text": "All tests pass."}},
                        "_meta": {"totalTokens": 70000}}),
                    notification("_x.ai/session/prompt_complete", {}),
                    response(42, {"_meta": {"totalTokens": 70000,
                                            "modelId": "mock", "stopReason": "end_turn"}}),
                ]
                for f in phase2:
                    reader.feed_data((json.dumps(f) + "\n").encode())

            # Run feeder and collector concurrently
            feeder = asyncio.create_task(feed_frames())
            speech, thoughts, meta = await asdaaas.collect_response(
                reader, prompt_id=42,
                keepalive_timeout=0.5,
                max_wall_clock=5.0)
            await feeder
            return speech, thoughts, meta

        speech, thoughts, meta = asyncio.run(_run())

        # BUG: speech == "Let me build the dashboard:" (only phase 1)
        # FIXED: speech includes ALL chunks from both phases
        assert speech == "Let me build the dashboard: Done. All tests pass.", \
            f"Speech loss! Got: '{speech}' — phase 2 speech was lost to keepalive timeout"
        assert meta.get("totalTokens") == 70000
