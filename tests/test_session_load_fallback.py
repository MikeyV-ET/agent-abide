"""Roster UUID is the spine: load it or fail. Never session/new over it.

Regression (df54358): missing sessionId minted a replacement and silent-forked
Squiggy/Trip-G. Operator-chosen session stays; RPC error refuses start.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from grok_backend import resolve_session_after_rpc


def test_resolve_keeps_requested_when_result_has_no_sessionId():
    requested = "019f4394-8693-7bf1-9c5f-02cf0950f44e"
    resp = {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"models": {"currentModelId": "grok-4.6"}},
    }
    assert resolve_session_after_rpc(requested=requested, resp=resp) == requested


def test_resolve_keeps_matching_sessionId():
    sid = "existing-good-id"
    resp = {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": sid}}
    assert resolve_session_after_rpc(requested=sid, resp=resp) == sid


def test_resolve_load_error_refuses():
    with pytest.raises(RuntimeError, match="session/load failed"):
        resolve_session_after_rpc(
            requested="fake-hand-minted-id",
            resp={
                "jsonrpc": "2.0",
                "id": 2,
                "error": {"code": -32602, "message": "unknown session id"},
            },
        )


def test_resolve_wrong_id_refuses():
    with pytest.raises(RuntimeError, match="returned minted-id"):
        resolve_session_after_rpc(
            requested="roster-id",
            resp={"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "minted-id"}},
        )


def test_resolve_new_without_requested_requires_sessionId():
    assert (
        resolve_session_after_rpc(
            requested=None,
            resp={"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "brand-new"}},
        )
        == "brand-new"
    )
    with pytest.raises(RuntimeError, match="session/new failed"):
        resolve_session_after_rpc(
            requested=None,
            resp={"jsonrpc": "2.0", "id": 2, "result": {}},
        )


@pytest.mark.asyncio
async def test_session_load_unknown_id_refuses_start(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    from grok_backend import GrokBackend

    be = GrokBackend.__new__(GrokBackend)
    be._grok_binary = '/usr/bin/grok'
    be._yolo = True
    be._rpc_id = 0
    be._session_id = None
    be._model_id = "unknown"
    be._grok_sessions_dir = tmp_path / "sessions"
    be._grok_sessions_dir.mkdir()
    be._file_source = None
    be._native_bus = None
    be._hot_ingest = False
    be._agent_home = None
    be._agent_name = "Wend"
    be._pending_tool_calls = set()
    be._grok_binary = "/usr/bin/grok"
    be._yolo = True
    be._total_tokens = 0
    be._compaction_event = None
    be._compaction_tokens_before = None
    be._compaction_tokens_after = None
    be._stdout_task = None
    be._proc = MagicMock()
    be._send = AsyncMock()
    be._rpc_request = lambda method, params=None: {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {},
    }
    be._rpc_notification = lambda method, params=None: {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
    }

    responses = [
        # initialize ok
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        # session/load fails
        {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {"code": -32602, "message": "Invalid params", "data": "unknown session id"},
        },
        # session/new ok
        {"jsonrpc": "2.0", "id": 3, "result": {"sessionId": "real-session-abc"}},
    ]
    call_i = {"n": 0}

    async def wait_resp(rpc_id, timeout=30):
        r = responses[call_i["n"]]
        call_i["n"] += 1
        return r

    be._wait_for_response = wait_resp
    # skip FileEventSource open complexity — patch open path after session id set
    with patch.object(GrokBackend, "start", GrokBackend.start):
        # Call only the session portion by invoking start with mocks for subprocess
        pass

    # Directly exercise the fixed block via a slim helper if we extract one —
    # instead call a private method we add, or run start with heavy mocks.
    # Simpler: unit-test the decision logic by importing a small function.

    # Re-read start and run with patched create_subprocess and wait
    async def fake_subprocess(*a, **k):
        proc = AsyncMock()
        proc.stdin = AsyncMock()
        proc.stdout = AsyncMock()
        proc.stderr = AsyncMock()
        proc.pid = 12345
        return proc

    be._seed_tokens_from_session = lambda: None
    call_i["n"] = 0
    # rebuild responses for full start: init, load fail, new ok
    responses.clear()
    responses.extend([
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "error": {"code": -32602, "message": "Invalid params", "data": "unknown session id"},
        },
        {"jsonrpc": "2.0", "id": 3, "result": {"sessionId": "real-session-abc"}},
    ])

    with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
        with patch.object(GrokBackend, "_process_stdout", new_callable=AsyncMock):
            with pytest.raises(RuntimeError, match="session/load failed"):
                await GrokBackend.start(
                    be,
                    session_id="fake-hand-minted-id",
                    agent_cwd=str(tmp_path / "home"),
                    model="grok-4.6",
                    agent_name="Wend",
                )
    sent_methods = []
    for c in be._send.await_args_list:
        msg = c.args[0]
        if isinstance(msg, dict) and "method" in msg:
            sent_methods.append(msg["method"])
    assert "session/load" in sent_methods
    assert "session/new" not in sent_methods


@pytest.mark.asyncio
async def test_session_load_success_keeps_id(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    from grok_backend import GrokBackend
    from unittest.mock import AsyncMock, MagicMock, patch

    be = GrokBackend.__new__(GrokBackend)
    be._grok_binary = '/usr/bin/grok'
    be._yolo = True
    be._rpc_id = 0
    be._session_id = None
    be._model_id = "unknown"
    be._grok_sessions_dir = tmp_path / "sessions"
    be._grok_sessions_dir.mkdir()
    be._file_source = None
    be._native_bus = None
    be._hot_ingest = False
    be._agent_home = None
    be._agent_name = "Wend"
    be._pending_tool_calls = set()
    be._grok_binary = "/usr/bin/grok"
    be._yolo = True
    be._total_tokens = 0
    be._compaction_event = None
    be._send = AsyncMock()
    be._rpc_request = lambda method, params=None: {
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
    }
    be._rpc_notification = lambda method, params=None: {
        "jsonrpc": "2.0", "method": method, "params": params or {},
    }
    responses = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "existing-good-id"}},
    ]
    call_i = {"n": 0}

    async def wait_resp(rpc_id, timeout=30):
        r = responses[call_i["n"]]
        call_i["n"] += 1
        return r

    be._wait_for_response = wait_resp
    be._seed_tokens_from_session = lambda: None

    async def fake_subprocess(*a, **k):
        proc = AsyncMock()
        proc.stdin = AsyncMock()
        proc.stdout = AsyncMock()
        proc.stderr = AsyncMock()
        proc.pid = 1
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
        with patch.object(GrokBackend, "_process_stdout", new_callable=AsyncMock):
            sid = await GrokBackend.start(
                be,
                session_id="existing-good-id",
                agent_cwd=str(tmp_path / "home"),
                model="grok-4.6",
                agent_name="Wend",
            )
    assert sid == "existing-good-id"


@pytest.mark.asyncio
async def test_session_load_no_sessionId_in_result_keeps_requested(tmp_path):
    from grok_backend import GrokBackend

    requested = "019f4394-8693-7bf1-9c5f-02cf0950f44e"
    be = GrokBackend.__new__(GrokBackend)
    be._grok_binary = "/usr/bin/grok"
    be._yolo = True
    be._rpc_id = 0
    be._session_id = None
    be._model_id = "unknown"
    be._grok_sessions_dir = tmp_path / "sessions"
    be._grok_sessions_dir.mkdir()
    be._file_source = None
    be._native_bus = None
    be._hot_ingest = False
    be._agent_home = None
    be._agent_name = "Squiggy"
    be._pending_tool_calls = set()
    be._total_tokens = 0
    be._compaction_event = None
    be._send = AsyncMock()
    be._rpc_request = lambda method, params=None: {
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
    }
    be._rpc_notification = lambda method, params=None: {
        "jsonrpc": "2.0", "method": method, "params": params or {},
    }
    responses = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"models": {"currentModelId": "grok-4.6"}}},
    ]
    call_i = {"n": 0}

    async def wait_resp(rpc_id, timeout=30):
        r = responses[call_i["n"]]
        call_i["n"] += 1
        return r

    be._wait_for_response = wait_resp
    be._seed_tokens_from_session = lambda: None

    async def fake_subprocess(*a, **k):
        proc = AsyncMock()
        proc.stdin = AsyncMock()
        proc.stdout = AsyncMock()
        proc.stderr = AsyncMock()
        proc.pid = 1
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
        with patch.object(GrokBackend, "_process_stdout", new_callable=AsyncMock):
            sid = await GrokBackend.start(
                be,
                session_id=requested,
                agent_cwd=str(tmp_path / "home"),
                model="grok-4.6",
                agent_name="Squiggy",
            )
    assert sid == requested
    sent_methods = []
    for c in be._send.await_args_list:
        msg = c.args[0]
        if isinstance(msg, dict) and "method" in msg:
            sent_methods.append(msg["method"])
    assert "session/load" in sent_methods
    assert "session/new" not in sent_methods
