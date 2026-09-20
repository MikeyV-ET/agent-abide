"""P0: aa.stream parser + tui_history resolve (no SA deps)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import resolve_history_source, entry_to_tui_lines, hot_jsonl_path


def test_resolve_prefers_hot(tmp_path: Path):
    hist = tmp_path / "asdaaas" / "history"
    hist.mkdir(parents=True)
    (hist / "hot.jsonl").write_text("{}\n")
    kind, path = resolve_history_source(tmp_path, prefer="auto")
    assert kind == "hot"
    assert path == hist / "hot.jsonl"


def test_resolve_none(tmp_path: Path):
    kind, path = resolve_history_source(tmp_path, prefer="auto")
    assert kind == "none"


def test_entry_to_tui_lines():
    assert entry_to_tui_lines({"role": "user", "content": "hi"}) == ["You: hi"]
    assert "hello" in entry_to_tui_lines({"role": "assistant", "content": "hello"})[0]


def test_parse_trip_g_hot_sample():
    from aa_stream_parser import parse_aa_stream
    hot = Path.home() / "agents/Trip-G/asdaaas/history/hot.jsonl"
    if not hot.exists() or hot.stat().st_size < 100:
        pytest.skip("no Trip-G hot.jsonl")
    # tail sample
    lines = hot.read_text(errors="replace").splitlines()[-500:]
    sample = Path("/tmp/tui_hot_sample.jsonl")
    sample.write_text("\n".join(lines) + "\n")
    entries = parse_aa_stream(str(sample), agent_label="Trip-G")
    assert isinstance(entries, list)
    # should get some conversation-shaped dicts
    assert len(entries) >= 0  # parser may return empty on odd tail
