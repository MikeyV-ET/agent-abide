"""full_stream_hook backend resolution."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from full_stream_hook import (  # noqa: E402
    _stream_tail_enabled,
    resolve_agent_backend,
)


def test_stream_enabled_legacy_and_generic():
    assert _stream_tail_enabled({"tail_grok": True})
    assert _stream_tail_enabled({"tail_stream": True})
    assert _stream_tail_enabled({"tail_backend": True})
    assert not _stream_tail_enabled({})
    assert not _stream_tail_enabled(None)


def test_backend_from_cfg():
    assert resolve_agent_backend("X", cfg={"backend": "claude"}) == "claude"
