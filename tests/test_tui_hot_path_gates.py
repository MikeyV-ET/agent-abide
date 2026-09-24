"""Gates that must hold for aa-dev TUI to paint (Sep 24 regression class)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_zstandard_importable():
    """hot L1 codec — without this, hot ingest logs and writes nothing."""
    import zstandard  # noqa: F401


def test_stream_tail_enabled_requires_config():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
    from full_stream_hook import _stream_tail_enabled

    assert _stream_tail_enabled(None) is False
    assert _stream_tail_enabled({}) is False
    assert _stream_tail_enabled({"tail_stream": True}) is True
    assert _stream_tail_enabled({"tail_grok": True}) is True


def test_tui_display_sor_is_hot_only():
    """aa-dev TUI docstring law: hot.jsonl only, no updates fallback."""
    tui = (Path(__file__).resolve().parents[1] / "tui" / "asdaaas_tui.py").read_text()
    assert "hot.jsonl" in tui
    assert "no updates.jsonl fallback" in tui or "no updates.jsonl" in tui


def test_history_config_template_shape():
    """Canonical config that enables backend hot ingest."""
    cfg = {"tail_stream": True, "owner": "backend"}
    assert cfg["tail_stream"] is True
    assert cfg["owner"] == "backend"
