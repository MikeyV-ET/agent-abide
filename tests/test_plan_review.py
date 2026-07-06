"""Tests for plan review auto-approval in GrokBackend.

Verifies that when the grok binary sends _x.ai/exit_plan_mode
JSON-RPC request, GrokBackend auto-approves it (headless mode).
Also tests the unhandled-request catch-all logging.
"""

import asyncio
import json
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))

from grok_backend import GrokBackend


class FakeProcess:
    """Minimal fake asyncio.subprocess.Process for testing _process_stdout."""

    def __init__(self, lines: list[bytes]):
        self.stdout = FakeStreamReader(lines)
        self.stdin = FakeStreamWriter()

    @property
    def responses_sent(self) -> list[dict]:
        return self.stdin.written_frames


class FakeStreamReader:
    """Feeds pre-loaded lines to _process_stdout one at a time."""

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)
        self._index = 0

    async def read(self, n: int) -> bytes:
        if self._index < len(self._lines):
            data = self._lines[self._index]
            self._index += 1
            return data
        return b""


class FakeStreamWriter:
    """Captures what _process_stdout sends back to the binary."""

    def __init__(self):
        self.written_frames: list[dict] = []
        self._raw: list[bytes] = []

    def write(self, data: bytes):
        self._raw.append(data)
        text = data.decode("utf-8").strip()
        if text:
            self.written_frames.append(json.loads(text))

    async def drain(self):
        pass


def _make_backend_with_fake_proc(lines: list[bytes]) -> tuple[GrokBackend, FakeProcess]:
    """Create a GrokBackend with a fake process injected."""
    backend = GrokBackend()
    fake_proc = FakeProcess(lines)
    backend._proc = fake_proc
    return backend, fake_proc


def _jsonrpc_request(method: str, rpc_id: int, params: dict = None) -> bytes:
    """Build a JSON-RPC request line (as the binary would send)."""
    msg = {"jsonrpc": "2.0", "method": method, "id": rpc_id}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg).encode("utf-8") + b"\n"


# ---- Tests ----

@pytest.mark.asyncio
async def test_plan_review_auto_approves():
    """When binary sends _x.ai/exit_plan_mode, backend responds with approve."""
    lines = [_jsonrpc_request("_x.ai/exit_plan_mode", 42)]
    backend, fake_proc = _make_backend_with_fake_proc(lines)

    await backend._process_stdout()

    assert len(fake_proc.responses_sent) == 1
    resp = fake_proc.responses_sent[0]
    assert resp["id"] == 42
    assert resp["result"]["outcome"]["outcome"] == "selected"
    assert resp["result"]["outcome"]["optionId"] == "approve"


@pytest.mark.asyncio
async def test_plan_review_with_params():
    """Plan review request with params is still auto-approved."""
    lines = [_jsonrpc_request("_x.ai/exit_plan_mode", 99,
                              {"planFile": "/tmp/plan.md"})]
    backend, fake_proc = _make_backend_with_fake_proc(lines)

    await backend._process_stdout()

    assert len(fake_proc.responses_sent) == 1
    resp = fake_proc.responses_sent[0]
    assert resp["id"] == 99
    assert resp["result"]["outcome"]["optionId"] == "approve"


@pytest.mark.asyncio
async def test_permission_and_plan_review_both_handled():
    """Both permission and plan review requests in one stream are handled."""
    lines = [
        _jsonrpc_request("session/request_permission", 10,
                         {"toolCall": {"kind": "bash"}}),
        _jsonrpc_request("_x.ai/exit_plan_mode", 11),
    ]
    backend, fake_proc = _make_backend_with_fake_proc(lines)
    # No permission handler set — defaults to reject-once

    await backend._process_stdout()

    assert len(fake_proc.responses_sent) == 2
    # First response: permission (rejected, no handler)
    perm_resp = fake_proc.responses_sent[0]
    assert perm_resp["id"] == 10
    assert perm_resp["result"]["outcome"]["optionId"] == "reject-once"
    # Second response: plan review (approved)
    plan_resp = fake_proc.responses_sent[1]
    assert plan_resp["id"] == 11
    assert plan_resp["result"]["outcome"]["optionId"] == "approve"


@pytest.mark.asyncio
async def test_unhandled_request_logged(capsys):
    """Unknown JSON-RPC request with id logs warning, no response sent."""
    lines = [_jsonrpc_request("session/unknown_method", 77)]
    backend, fake_proc = _make_backend_with_fake_proc(lines)

    await backend._process_stdout()

    # No response sent for unhandled requests
    assert len(fake_proc.responses_sent) == 0
    # But it was logged
    captured = capsys.readouterr()
    assert "unhandled request" in captured.out
    assert "session/unknown_method" in captured.out
    assert "77" in captured.out


@pytest.mark.asyncio
async def test_notification_without_id_not_logged(capsys):
    """JSON-RPC notification (no id) with unknown method is silently ignored."""
    # Notification has no "id" field
    msg = json.dumps({"jsonrpc": "2.0", "method": "some/notification"}).encode() + b"\n"
    lines = [msg]
    backend, fake_proc = _make_backend_with_fake_proc(lines)

    await backend._process_stdout()

    assert len(fake_proc.responses_sent) == 0
    captured = capsys.readouterr()
    assert "unhandled request" not in captured.out


@pytest.mark.asyncio
async def test_malformed_json_skipped():
    """Malformed JSON lines are skipped without crashing."""
    lines = [
        b"not json at all\n",
        _jsonrpc_request("_x.ai/exit_plan_mode", 5),
    ]
    backend, fake_proc = _make_backend_with_fake_proc(lines)

    await backend._process_stdout()

    assert len(fake_proc.responses_sent) == 1
    assert fake_proc.responses_sent[0]["id"] == 5


# ---- ask_user_question tests ----

@pytest.mark.asyncio
async def test_ask_user_auto_selects():
    """When binary sends _x.ai/ask_user_question, backend auto-responds."""
    lines = [_jsonrpc_request("_x.ai/ask_user_question", 50,
                              {"question": "Which approach?", "options": ["A", "B"]})]
    backend, fake_proc = _make_backend_with_fake_proc(lines)

    await backend._process_stdout()

    assert len(fake_proc.responses_sent) == 1
    resp = fake_proc.responses_sent[0]
    assert resp["id"] == 50
    assert resp["result"]["outcome"]["outcome"] == "selected"
    assert resp["result"]["outcome"]["optionId"] == "approve"


@pytest.mark.asyncio
async def test_ask_user_with_no_params():
    """ask_user_question with no params still auto-responds."""
    lines = [_jsonrpc_request("_x.ai/ask_user_question", 60)]
    backend, fake_proc = _make_backend_with_fake_proc(lines)

    await backend._process_stdout()

    assert len(fake_proc.responses_sent) == 1
    assert fake_proc.responses_sent[0]["id"] == 60
    assert fake_proc.responses_sent[0]["result"]["outcome"]["optionId"] == "approve"


@pytest.mark.asyncio
async def test_all_three_gates_in_stream():
    """Permission, plan review, and ask_user all handled in one stream."""
    lines = [
        _jsonrpc_request("session/request_permission", 1,
                         {"toolCall": {"kind": "bash"}}),
        _jsonrpc_request("_x.ai/exit_plan_mode", 2),
        _jsonrpc_request("_x.ai/ask_user_question", 3,
                         {"question": "Continue?"}),
    ]
    backend, fake_proc = _make_backend_with_fake_proc(lines)

    await backend._process_stdout()

    assert len(fake_proc.responses_sent) == 3
    assert fake_proc.responses_sent[0]["id"] == 1  # permission
    assert fake_proc.responses_sent[0]["result"]["outcome"]["optionId"] == "reject-once"
    assert fake_proc.responses_sent[1]["id"] == 2  # plan review
    assert fake_proc.responses_sent[1]["result"]["outcome"]["optionId"] == "approve"
    assert fake_proc.responses_sent[2]["id"] == 3  # ask user
    assert fake_proc.responses_sent[2]["result"]["outcome"]["optionId"] == "approve"


@pytest.mark.asyncio
async def test_unhandled_request_includes_params(capsys):
    """Unhandled request log includes truncated params."""
    lines = [_jsonrpc_request("_x.ai/unknown_gate", 88,
                              {"detail": "some context"})]
    backend, fake_proc = _make_backend_with_fake_proc(lines)

    await backend._process_stdout()

    assert len(fake_proc.responses_sent) == 0
    captured = capsys.readouterr()
    assert "unhandled request" in captured.out
    assert "_x.ai/unknown_gate" in captured.out
    assert "some context" in captured.out
