"""Collect must not keepalive-timeout while tool calls are still open."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from grok_backend import GrokBackend  # noqa: E402


def test_keepalive_waits_while_tools_pending():
    async def _run():
        b = GrokBackend.__new__(GrokBackend)
        b._permission_pending = False
        b._pending_tool_calls = {"call-open"}
        b._hot_ingest = False
        b._total_tokens = 0
        b._model_id = "x"
        b._last_activity_ts = time.time()
        b._compaction_event = None
        b._file_source = type("FS", (), {"read_new_lines": lambda self: ([], [])})()

        n = {"i": 0}
        real_sleep = asyncio.sleep

        async def fake_sleep(_s):
            n["i"] += 1
            if n["i"] >= 8:
                b._pending_tool_calls.clear()
            await real_sleep(0.005)

        import asyncio as aio

        orig = aio.sleep
        aio.sleep = fake_sleep
        try:
            r = await b._collect_from_files(keepalive_timeout=0.02, max_wall_clock=3.0)
        finally:
            aio.sleep = orig

        assert n["i"] >= 8, n["i"]
        assert r.stop_reason == "keepalive_timeout"
        return r

    asyncio.run(_run())
