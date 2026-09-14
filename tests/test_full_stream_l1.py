"""V2 history L1 unit tests."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

# skip if no zstandard
zstd = pytest.importorskip("zstandard")

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from full_stream import (
    HOT_KEEP_BYTES,
    agent_full_stream_dir,
    ensure_layout,
    find_prune_cut,
    plan_prune_hot,
    prune_hot,
    read_manifest,
    seal_byte_range,
    verify_chunk,
    open_chunk_lines,
    chunks_covering_ts,
    rewrite_hot_tail,
)


def _write_updates(path: Path, n: int, start_ts: float = 1_700_000_000.0) -> None:
    with open(path, "w") as f:
        for i in range(n):
            obj = {
                "timestamp": start_ts + i,
                "params": {"update": {"sessionUpdate": "agent_message_chunk", "content": f"line-{i} " * 20}},
            }
            f.write(json.dumps(obj) + "\n")


def test_seal_and_verify(tmp_path: Path):
    home = tmp_path / "AgentX"
    (home / "asdaaas").mkdir(parents=True)
    fs = agent_full_stream_dir(home)
    ensure_layout(fs)
    src = tmp_path / "updates.jsonl"
    _write_updates(src, 100)
    rec = seal_byte_range(src, fs, agent="AgentX", session_id="sess1", source_label="test")
    assert rec.line_count == 100
    assert rec.plain_bytes == src.stat().st_size
    assert rec.zstd_bytes < rec.plain_bytes
    assert verify_chunk(fs, rec)
    man = read_manifest(fs)
    assert len(man) == 1
    assert man[0].chunk_id == rec.chunk_id
    lines = list(open_chunk_lines(fs, rec))
    assert len(lines) == 100


def test_chunks_covering_ts(tmp_path: Path):
    home = tmp_path / "A"
    (home / "asdaaas").mkdir(parents=True)
    fs = agent_full_stream_dir(home)
    src = tmp_path / "u.jsonl"
    _write_updates(src, 50, start_ts=1000.0)
    rec = seal_byte_range(src, fs)
    assert chunks_covering_ts([rec], 1005, 1010)
    assert not chunks_covering_ts([rec], 0, 10)
    assert not chunks_covering_ts([rec], 2000, 3000)


def test_prune_cut_line_aligned(tmp_path: Path):
    src = tmp_path / "u.jsonl"
    # build ~ file with known size
    _write_updates(src, 500)
    size = src.stat().st_size
    keep = size // 3
    cut = find_prune_cut(src, keep_bytes=keep)
    assert 0 < cut < size
    # cut is line boundary
    data = src.read_bytes()
    assert data[cut - 1 : cut] == b"\n" or cut == 0
    tail = data[cut:]
    assert abs(len(tail) - keep) < 5000  # within a few lines


def test_prune_hot_dry_and_apply(tmp_path: Path):
    home = tmp_path / "A"
    (home / "asdaaas").mkdir(parents=True)
    fs = agent_full_stream_dir(home)
    hot = tmp_path / "hot.jsonl"
    # small artificial thresholds
    _write_updates(hot, 400)
    size = hot.stat().st_size
    max_b = size // 2
    keep_b = size // 4
    plan = plan_prune_hot(hot, max_bytes=max_b, keep_bytes=keep_b)
    assert plan["needed"] is True
    dry = prune_hot(hot, fs, apply=False, max_bytes=max_b, keep_bytes=keep_b)
    assert dry["status"] == "dry_run"
    assert hot.stat().st_size == size  # unchanged
    result = prune_hot(
        hot, fs, apply=True, agent="A", session_id="s", max_bytes=max_b, keep_bytes=keep_b
    )
    assert result["status"] == "pruned"
    assert hot.stat().st_size == result["tail"]["size_after"]
    assert hot.stat().st_size <= keep_b + 5000
    assert verify_chunk(fs, read_manifest(fs)[0])
    # sealed prefix lines + tail lines ≈ original
    rec = read_manifest(fs)[0]
    sealed_lines = rec.line_count
    tail_lines = sum(1 for _ in open(hot))
    assert sealed_lines + tail_lines == 400


def test_rewrite_tail_dry(tmp_path: Path):
    p = tmp_path / "f.jsonl"
    p.write_text("a\nb\nc\n")
    plan = rewrite_hot_tail(p, 2, apply=False)
    assert plan["apply"] is False
    assert p.read_text() == "a\nb\nc\n"


def test_resolve_prefers_history_over_legacy(tmp_path):
    from full_stream import resolve_history_dir, agent_history_dir, HISTORY_DIRNAME, LEGACY_HISTORY_DIRNAME
    home = tmp_path / "agent"
    leg = home / "asdaaas" / LEGACY_HISTORY_DIRNAME
    hist = home / "asdaaas" / HISTORY_DIRNAME
    leg.mkdir(parents=True)
    (leg / "hot.jsonl").write_text("")
    assert resolve_history_dir(home) == leg
    hist.mkdir(parents=True)
    (hist / "hot.jsonl").write_text("")
    assert resolve_history_dir(home) == hist
    assert agent_history_dir(home) == hist
