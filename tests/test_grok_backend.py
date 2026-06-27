"""Tests for FileEventSource and GrokBackend response processing.

Covers: FileEventSource (open, read_new_lines, close),
        _process_update_frames, _collect_from_files, drain_stale.

Ported from mikeyv-infra/tests/test_grok_backend.py to agent-abide.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add core directory to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from grok_backend import FileEventSource, GrokBackend, POST_TURN_DRAIN_DELAY_S
from agent_backend import ResponseResult, TurnCancelled


# ============================================================================
# Helpers
# ============================================================================

def write_jsonl(path: Path, records: list[dict]):
    """Append JSON lines to a file."""
    with open(path, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def make_update_frame(session_update: str, content: dict = None, meta: dict = None):
    """Build a frame as it appears in updates.jsonl."""
    frame = {"params": {"update": {"sessionUpdate": session_update}}}
    if content:
        frame["params"]["update"]["content"] = content
    if meta:
        frame["params"]["_meta"] = meta
    # Tool call frames carry extra fields at update level
    return frame


def make_speech_frame(text: str, meta: dict = None):
    return make_update_frame("agent_message_chunk", content={"text": text}, meta=meta)


def make_thought_frame(text: str):
    return make_update_frame("agent_thought_chunk", content={"text": text})


def make_tool_call_frame(tool_id: str, title: str = "bash"):
    frame = make_update_frame("tool_call")
    frame["params"]["update"]["toolCallId"] = tool_id
    frame["params"]["update"]["title"] = title
    return frame


def make_tool_complete_frame(tool_id: str):
    frame = make_update_frame("tool_call_update")
    frame["params"]["update"]["toolCallId"] = tool_id
    frame["params"]["update"]["status"] = "completed"
    return frame


def make_turn_ended(outcome: str = "completed"):
    return {"type": "turn_ended", "outcome": outcome}


def make_turn_started():
    return {"type": "turn_started"}


# ============================================================================
# FileEventSource unit tests
# ============================================================================

class TestFileEventSource:
    def test_open_seeks_to_end(self, tmp_path):
        """open() should seek past existing content so only new lines are read."""
        updates = tmp_path / "updates.jsonl"
        events = tmp_path / "events.jsonl"
        write_jsonl(updates, [make_speech_frame("old")])
        write_jsonl(events, [make_turn_started()])

        src = FileEventSource(tmp_path)
        src.open()
        u, e = src.read_new_lines()
        assert u == []
        assert e == []
        src.close()

    def test_read_new_lines_returns_new_content(self, tmp_path):
        """Lines written after open() are returned."""
        updates = tmp_path / "updates.jsonl"
        events = tmp_path / "events.jsonl"
        updates.touch()
        events.touch()

        src = FileEventSource(tmp_path)
        src.open()

        write_jsonl(updates, [make_speech_frame("hello")])
        write_jsonl(events, [make_turn_ended()])

        u, e = src.read_new_lines()
        assert len(u) == 1
        assert u[0]["params"]["update"]["content"]["text"] == "hello"
        assert len(e) == 1
        assert e[0]["type"] == "turn_ended"
        src.close()

    def test_read_new_lines_handles_partial_write(self, tmp_path):
        """A line without a trailing newline is not returned (partial write)."""
        updates = tmp_path / "updates.jsonl"
        events = tmp_path / "events.jsonl"
        updates.touch()
        events.touch()

        src = FileEventSource(tmp_path)
        src.open()

        # Write a partial line (no trailing newline)
        with open(updates, "a") as f:
            f.write('{"params": {"update": {"sessionUpdate": "agent_message_chunk"')
        u, _ = src.read_new_lines()
        # Python file iteration reads lines; a line without \n at EOF
        # is still returned by the iterator, but it's incomplete JSON
        # so json.loads will fail and it gets skipped
        # Actually, the behavior depends on whether the file ends with \n
        # Let's verify: the JSONDecodeError path handles this
        assert len(u) == 0  # malformed JSON is skipped
        src.close()

    def test_read_new_lines_skips_bad_json(self, tmp_path):
        """Malformed JSON lines are skipped, valid ones returned."""
        updates = tmp_path / "updates.jsonl"
        events = tmp_path / "events.jsonl"
        updates.touch()
        events.touch()

        src = FileEventSource(tmp_path)
        src.open()

        with open(updates, "a") as f:
            f.write("not json\n")
            f.write(json.dumps(make_speech_frame("good")) + "\n")
        u, _ = src.read_new_lines()
        assert len(u) == 1
        assert u[0]["params"]["update"]["content"]["text"] == "good"
        src.close()

    def test_read_new_lines_multiple_reads(self, tmp_path):
        """Multiple read calls return only new lines each time."""
        updates = tmp_path / "updates.jsonl"
        events = tmp_path / "events.jsonl"
        updates.touch()
        events.touch()

        src = FileEventSource(tmp_path)
        src.open()

        write_jsonl(updates, [make_speech_frame("first")])
        u1, _ = src.read_new_lines()
        assert len(u1) == 1

        write_jsonl(updates, [make_speech_frame("second")])
        u2, _ = src.read_new_lines()
        assert len(u2) == 1
        assert u2[0]["params"]["update"]["content"]["text"] == "second"
        src.close()

    def test_close_nulls_handles(self, tmp_path):
        """close() sets file handles to None."""
        updates = tmp_path / "updates.jsonl"
        events = tmp_path / "events.jsonl"
        updates.touch()
        events.touch()

        src = FileEventSource(tmp_path)
        src.open()
        assert src._updates_fp is not None
        assert src._events_fp is not None
        src.close()
        assert src._updates_fp is None
        assert src._events_fp is None

    def test_read_empty_files(self, tmp_path):
        """Reading from empty files returns empty lists."""
        updates = tmp_path / "updates.jsonl"
        events = tmp_path / "events.jsonl"
        updates.touch()
        events.touch()

        src = FileEventSource(tmp_path)
        src.open()
        u, e = src.read_new_lines()
        assert u == []
        assert e == []
        src.close()


# ============================================================================
# _process_update_frames tests
# ============================================================================

class TestProcessUpdateFrames:
    """Test GrokBackend._process_update_frames in isolation."""

    def _make_backend(self):
        backend = GrokBackend.__new__(GrokBackend)
        backend._total_tokens = 0
        backend._model_id = "test-model"
        backend._compaction_event = None
        backend._compaction_tokens_before = 0
        backend._compaction_tokens_after = 0
        backend._last_activity_ts = 0.0
        backend._pending_tool_calls = set()
        return backend

    def test_speech_chunks(self):
        backend = self._make_backend()
        speech = []
        thoughts = []
        pending = set()
        frames = [make_speech_frame("Hello "), make_speech_frame("world")]
        backend._process_update_frames(frames, speech, thoughts, pending, None, None, None)
        assert speech == ["Hello ", "world"]

    def test_thought_chunks(self):
        backend = self._make_backend()
        speech = []
        thoughts = []
        pending = set()
        frames = [make_thought_frame("thinking...")]
        backend._process_update_frames(frames, speech, thoughts, pending, None, None, None)
        assert thoughts == ["thinking..."]

    def test_tool_call_tracking(self):
        backend = self._make_backend()
        speech = []
        thoughts = []
        pending = set()
        frames = [make_tool_call_frame("tc_1", "read_file")]
        backend._process_update_frames(frames, speech, thoughts, pending, None, None, None)
        assert "tc_1" in pending

    def test_tool_call_completion(self):
        backend = self._make_backend()
        speech = []
        thoughts = []
        pending = {"tc_1"}
        frames = [make_tool_complete_frame("tc_1")]
        backend._process_update_frames(frames, speech, thoughts, pending, None, None, None)
        assert "tc_1" not in pending

    def test_meta_token_tracking(self):
        backend = self._make_backend()
        speech = []
        thoughts = []
        pending = set()
        frames = [make_speech_frame("hi", meta={"totalTokens": 42000})]
        backend._process_update_frames(frames, speech, thoughts, pending, None, None, None)
        assert backend._total_tokens == 42000

    def test_callbacks_fire(self):
        backend = self._make_backend()
        speech = []
        thoughts = []
        pending = set()

        speech_cb = MagicMock()
        tool_cb = MagicMock()
        meta_cb = MagicMock()

        frames = [
            make_speech_frame("hi", meta={"totalTokens": 1000}),
            make_tool_call_frame("tc_1", "bash"),
        ]
        backend._process_update_frames(
            frames, speech, thoughts, pending,
            speech_cb, tool_cb, meta_cb,
        )
        speech_cb.assert_called_once_with("hi")
        tool_cb.assert_called_once_with("bash")
        meta_cb.assert_called_once_with(1000)

    def test_tool_call_adds_newline_separator(self):
        """Tool call appends \\n\\n to speech if last chunk doesn't end with it."""
        backend = self._make_backend()
        speech = ["some text"]
        thoughts = []
        pending = set()
        frames = [make_tool_call_frame("tc_1")]
        backend._process_update_frames(frames, speech, thoughts, pending, None, None, None)
        assert speech[-1] == "\n\n"

    def test_empty_text_not_appended(self):
        """Speech frames with empty text are not appended."""
        backend = self._make_backend()
        speech = []
        thoughts = []
        pending = set()
        frame = make_update_frame("agent_message_chunk", content={"text": ""})
        backend._process_update_frames([frame], speech, thoughts, pending, None, None, None)
        assert speech == []


# ============================================================================
# _collect_from_files tests (async)
# ============================================================================

class TestCollectFromFiles:
    """Integration tests for _collect_from_files using real temp files."""

    def _make_backend(self, tmp_path):
        backend = GrokBackend.__new__(GrokBackend)
        backend._total_tokens = 0
        backend._model_id = "test-model"
        backend._permission_pending = False
        backend._compaction_event = None
        backend._compaction_tokens_before = 0
        backend._compaction_tokens_after = 0
        backend._last_activity_ts = 0.0

        updates = tmp_path / "updates.jsonl"
        events = tmp_path / "events.jsonl"
        updates.touch()
        events.touch()

        backend._file_source = FileEventSource(tmp_path)
        backend._file_source.open()
        return backend

    @pytest.mark.asyncio
    async def test_basic_turn(self, tmp_path):
        """Speech + turn_ended produces a complete ResponseResult."""
        backend = self._make_backend(tmp_path)

        # Write content and turn_ended after a brief delay
        async def write_later():
            await asyncio.sleep(0.1)
            write_jsonl(tmp_path / "updates.jsonl", [
                make_speech_frame("Hello from agent"),
            ])
            write_jsonl(tmp_path / "events.jsonl", [make_turn_ended()])

        asyncio.create_task(write_later())
        result = await backend._collect_from_files(keepalive_timeout=5.0, max_wall_clock=5.0)
        assert result.speech == "Hello from agent"
        assert result.stop_reason == "completed"
        backend._file_source.close()

    @pytest.mark.asyncio
    async def test_keepalive_timeout(self, tmp_path):
        """No activity for keepalive_timeout triggers timeout."""
        backend = self._make_backend(tmp_path)
        result = await backend._collect_from_files(keepalive_timeout=0.2, max_wall_clock=5.0)
        assert result.stop_reason == "keepalive_timeout"
        backend._file_source.close()

    @pytest.mark.asyncio
    async def test_wall_clock_timeout(self, tmp_path):
        """Activity that never ends hits wall clock."""
        backend = self._make_backend(tmp_path)

        # Keep writing updates but never send turn_ended
        async def write_forever():
            for i in range(100):
                write_jsonl(tmp_path / "updates.jsonl", [make_speech_frame(f"chunk{i}")])
                await asyncio.sleep(0.05)

        task = asyncio.create_task(write_forever())
        result = await backend._collect_from_files(keepalive_timeout=10.0, max_wall_clock=0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert result.stop_reason == "wall_clock_timeout"
        backend._file_source.close()

    @pytest.mark.asyncio
    async def test_cancel_event(self, tmp_path):
        """Setting cancel_event raises TurnCancelled."""
        backend = self._make_backend(tmp_path)
        cancel = asyncio.Event()

        async def cancel_later():
            await asyncio.sleep(0.1)
            cancel.set()

        asyncio.create_task(cancel_later())
        with pytest.raises(TurnCancelled):
            await backend._collect_from_files(
                keepalive_timeout=5.0, max_wall_clock=5.0, cancel_event=cancel,
            )
        backend._file_source.close()

    @pytest.mark.asyncio
    async def test_turn_ended_outcome_passed_through(self, tmp_path):
        """turn_ended outcome field becomes stop_reason."""
        backend = self._make_backend(tmp_path)

        async def write_later():
            await asyncio.sleep(0.1)
            write_jsonl(tmp_path / "events.jsonl", [make_turn_ended("compacted")])

        asyncio.create_task(write_later())
        result = await backend._collect_from_files(keepalive_timeout=5.0, max_wall_clock=5.0)
        assert result.stop_reason == "compacted"
        backend._file_source.close()

    @pytest.mark.asyncio
    async def test_post_turn_drain_catches_late_writes(self, tmp_path):
        """Updates written just before turn_ended are captured in the drain."""
        backend = self._make_backend(tmp_path)

        async def write_later():
            await asyncio.sleep(0.1)
            # Write speech and turn_ended almost simultaneously
            write_jsonl(tmp_path / "updates.jsonl", [make_speech_frame("late")])
            write_jsonl(tmp_path / "events.jsonl", [make_turn_ended()])

        asyncio.create_task(write_later())
        result = await backend._collect_from_files(keepalive_timeout=5.0, max_wall_clock=5.0)
        assert "late" in result.speech
        backend._file_source.close()

    @pytest.mark.asyncio
    async def test_token_count_updated(self, tmp_path):
        """_meta.totalTokens updates the backend's total_tokens."""
        backend = self._make_backend(tmp_path)

        async def write_later():
            await asyncio.sleep(0.1)
            write_jsonl(tmp_path / "updates.jsonl", [
                make_speech_frame("hi", meta={"totalTokens": 55000}),
            ])
            write_jsonl(tmp_path / "events.jsonl", [make_turn_ended()])

        asyncio.create_task(write_later())
        result = await backend._collect_from_files(keepalive_timeout=5.0, max_wall_clock=5.0)
        assert backend._total_tokens == 55000
        backend._file_source.close()


# ============================================================================
# drain_stale tests
# ============================================================================

class TestDrainStale:
    @pytest.mark.asyncio
    async def test_drain_returns_count_and_speech(self, tmp_path):
        backend = GrokBackend.__new__(GrokBackend)
        backend._total_tokens = 0
        backend._model_id = "test"
        backend._compaction_event = None
        backend._compaction_tokens_before = 0
        backend._compaction_tokens_after = 0
        backend._last_activity_ts = 0.0

        updates = tmp_path / "updates.jsonl"
        events = tmp_path / "events.jsonl"
        updates.touch()
        events.touch()

        backend._file_source = FileEventSource(tmp_path)
        backend._file_source.open()

        write_jsonl(updates, [make_speech_frame("stale speech")])
        write_jsonl(events, [make_turn_ended()])

        count, speech = await backend.drain_stale()
        assert count == 2  # 1 update + 1 event
        assert speech == "stale speech"
        backend._file_source.close()

    @pytest.mark.asyncio
    async def test_drain_with_no_source(self):
        backend = GrokBackend.__new__(GrokBackend)
        backend._file_source = None
        count, speech = await backend.drain_stale()
        assert count == 0
        assert speech == ""


# ============================================================================
# Permission handling tests
# ============================================================================

def _make_permission_request_frame(rpc_id: int, kind: str = "execute",
                                    title: str = "Running command",
                                    tool_call_id: str = "call_001"):
    """Build a session/request_permission JSON-RPC request frame."""
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "test-session",
            "toolCall": {
                "toolCallId": tool_call_id,
                "title": title,
                "kind": kind,
                "status": "pending",
            },
            "options": [
                {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "allow-always", "name": "Allow always", "kind": "allow_always"},
                {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
            ]
        }
    }


class TestPermissionHandling:
    """Tests for _handle_permission_request and _process_stdout."""

    def _make_backend(self):
        backend = GrokBackend.__new__(GrokBackend)
        backend._proc = None
        backend._session_id = "test-session"
        backend._rpc_id = 0
        backend._total_tokens = 0
        backend._context_window = 200000
        backend._model_id = "test"
        backend._file_source = None
        backend._stdout_task = None
        backend._permission_handler = None
        backend._allowed_always = set()
        backend._permission_pending = False
        backend._grok_sessions_dir = Path("/tmp")
        backend._grok_binary = "grok"
        return backend

    @pytest.mark.asyncio
    async def test_permission_request_with_handler(self):
        """Handler receives params and its return value becomes the option_id."""
        backend = self._make_backend()
        sent_responses = []

        async def mock_send(msg):
            sent_responses.append(json.loads(msg))

        backend._send = mock_send

        async def handler(params):
            assert params["toolCall"]["kind"] == "execute"
            return "allow-once"

        backend.set_permission_handler(handler)

        frame = _make_permission_request_frame(rpc_id=5)
        await backend._handle_permission_request(frame)

        assert len(sent_responses) == 1
        resp = sent_responses[0]
        assert resp["id"] == 5
        assert resp["result"]["outcome"]["optionId"] == "allow-once"

    @pytest.mark.asyncio
    async def test_permission_request_no_handler_rejects(self):
        """Without a handler, permission requests are auto-rejected."""
        backend = self._make_backend()
        sent_responses = []

        async def mock_send(msg):
            sent_responses.append(json.loads(msg))

        backend._send = mock_send

        frame = _make_permission_request_frame(rpc_id=7)
        await backend._handle_permission_request(frame)

        resp = sent_responses[0]
        assert resp["result"]["outcome"]["optionId"] == "reject-once"

    @pytest.mark.asyncio
    async def test_allow_always_caches_kind(self):
        """allow-always for a kind should auto-approve future requests of same kind."""
        backend = self._make_backend()
        sent_responses = []

        async def mock_send(msg):
            sent_responses.append(json.loads(msg))

        backend._send = mock_send

        call_count = 0

        async def handler(params):
            nonlocal call_count
            call_count += 1
            return "allow-always"

        backend.set_permission_handler(handler)

        # First request — handler called
        frame1 = _make_permission_request_frame(rpc_id=10, kind="read")
        await backend._handle_permission_request(frame1)
        assert call_count == 1
        assert "read" in backend._allowed_always

        # Second request same kind — handler NOT called (cached)
        frame2 = _make_permission_request_frame(rpc_id=11, kind="read")
        await backend._handle_permission_request(frame2)
        assert call_count == 1  # still 1
        assert sent_responses[1]["result"]["outcome"]["optionId"] == "allow-always"

    @pytest.mark.asyncio
    async def test_allow_always_different_kind_still_asks(self):
        """allow-always for 'read' doesn't auto-approve 'execute'."""
        backend = self._make_backend()
        sent_responses = []

        async def mock_send(msg):
            sent_responses.append(json.loads(msg))

        backend._send = mock_send

        call_count = 0

        async def handler(params):
            nonlocal call_count
            call_count += 1
            return "allow-always"

        backend.set_permission_handler(handler)

        # Approve read
        frame1 = _make_permission_request_frame(rpc_id=20, kind="read")
        await backend._handle_permission_request(frame1)
        assert call_count == 1

        # Execute is a different kind — handler called
        frame2 = _make_permission_request_frame(rpc_id=21, kind="execute")
        await backend._handle_permission_request(frame2)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_process_stdout_intercepts_permission(self):
        """_process_stdout parses JSON lines and routes permission requests."""
        backend = self._make_backend()
        sent_responses = []
        handled_params = []

        async def mock_send(msg):
            sent_responses.append(json.loads(msg))

        backend._send = mock_send

        async def handler(params):
            handled_params.append(params)
            return "allow-once"

        backend.set_permission_handler(handler)

        # Simulate stdout with a permission request + garbage
        perm_frame = _make_permission_request_frame(rpc_id=42, kind="edit",
                                                     title="Editing file")
        lines = (
            json.dumps({"id": 99, "result": {}}) + "\n"  # normal response (discarded)
            + json.dumps(perm_frame) + "\n"  # permission request
            + "not json\n"  # garbage (skipped)
        )

        # Create a mock process with stdout
        class MockStdout:
            def __init__(self, data):
                self._data = data.encode()
                self._pos = 0

            async def read(self, n):
                if self._pos >= len(self._data):
                    return b""
                chunk = self._data[self._pos:self._pos + n]
                self._pos += n
                return chunk

        class MockProc:
            def __init__(self, stdout):
                self.stdout = stdout

        backend._proc = MockProc(MockStdout(lines))

        await backend._process_stdout()

        assert len(handled_params) == 1
        assert handled_params[0]["toolCall"]["kind"] == "edit"
        assert len(sent_responses) == 1
        assert sent_responses[0]["id"] == 42

    @pytest.mark.asyncio
    async def test_reject_handler_response(self):
        """Handler returning reject-once sends rejection."""
        backend = self._make_backend()
        sent_responses = []

        async def mock_send(msg):
            sent_responses.append(json.loads(msg))

        backend._send = mock_send

        async def handler(params):
            return "reject-once"

        backend.set_permission_handler(handler)

        frame = _make_permission_request_frame(rpc_id=99)
        await backend._handle_permission_request(frame)

        assert sent_responses[0]["result"]["outcome"]["optionId"] == "reject-once"

    @pytest.mark.asyncio
    async def test_permission_pending_flag_set_during_handler(self):
        """_permission_pending is True while handler runs, False after."""
        backend = self._make_backend()
        sent_responses = []
        flag_during_handler = None

        async def mock_send(msg):
            sent_responses.append(json.loads(msg))

        backend._send = mock_send

        async def handler(params):
            nonlocal flag_during_handler
            flag_during_handler = backend._permission_pending
            return "allow-once"

        backend.set_permission_handler(handler)

        assert backend._permission_pending is False
        frame = _make_permission_request_frame(rpc_id=50)
        await backend._handle_permission_request(frame)
        assert flag_during_handler is True
        assert backend._permission_pending is False

    @pytest.mark.asyncio
    async def test_permission_pending_cleared_on_handler_error(self):
        """_permission_pending is cleared even if handler raises."""
        backend = self._make_backend()

        async def mock_send(msg):
            pass

        backend._send = mock_send

        async def handler(params):
            raise RuntimeError("handler crashed")

        backend.set_permission_handler(handler)

        frame = _make_permission_request_frame(rpc_id=51)
        with pytest.raises(RuntimeError):
            await backend._handle_permission_request(frame)
        assert backend._permission_pending is False


class TestPermissionHandlerFiles:
    """Tests for permission_handler.py file operations."""

    def test_write_and_read_request(self, tmp_path):
        import permission_handler as ph
        from permission_handler import (
            write_permission_request, list_pending, read_decision,
            approve_permission, reject_permission, archive_request,
        )
        orig = ph._permissions_dir
        ph._permissions_dir = lambda agent: tmp_path / agent / "asdaaas" / "permissions"

        try:
            params = {
                "sessionId": "sess-1",
                "toolCall": {
                    "toolCallId": "tc_001",
                    "kind": "execute",
                    "title": "Running rm -rf",
                    "status": "pending",
                    "content": [],
                },
                "options": [
                    {"optionId": "allow-once"},
                    {"optionId": "reject-once"},
                ],
            }

            req_id = write_permission_request("Intern", params)
            assert req_id.startswith("perm_")

            pending = list_pending("Intern")
            assert len(pending) == 1
            assert pending[0]["kind"] == "execute"
            assert pending[0]["title"] == "Running rm -rf"

            # No decision yet
            assert read_decision("Intern", req_id) is None

            # Approve
            approve_permission("Intern", req_id, reason="Looks safe", decided_by="Q")
            decision = read_decision("Intern", req_id)
            assert decision["decision"] == "allow-once"
            assert decision["decided_by"] == "Q"

            # Archive
            archive_request("Intern", req_id)
            assert len(list_pending("Intern")) == 0
            log_dir = tmp_path / "Intern" / "asdaaas" / "permissions" / "log"
            assert len(list(log_dir.glob("*.json"))) == 2  # pending + decision

        finally:
            ph._permissions_dir = orig

    def test_reject_permission(self, tmp_path):
        import permission_handler as ph
        from permission_handler import write_permission_request, read_decision, reject_permission
        orig = ph._permissions_dir
        ph._permissions_dir = lambda agent: tmp_path / agent / "asdaaas" / "permissions"

        try:
            params = {
                "sessionId": "s",
                "toolCall": {"toolCallId": "tc", "kind": "execute", "title": "bad"},
                "options": [],
            }
            req_id = write_permission_request("Intern", params)
            reject_permission("Intern", req_id, reason="Dangerous", decided_by="Jr")

            decision = read_decision("Intern", req_id)
            assert decision["decision"] == "reject-once"
            assert decision["reason"] == "Dangerous"
        finally:
            ph._permissions_dir = orig

    def test_list_pending_empty(self, tmp_path):
        import permission_handler as ph
        from permission_handler import list_pending
        orig = ph._permissions_dir
        ph._permissions_dir = lambda agent: tmp_path / agent / "asdaaas" / "permissions"

        try:
            assert list_pending("NobodyHere") == []
        finally:
            ph._permissions_dir = orig
