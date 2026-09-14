"""V1 token-efficient transcript generator."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
from v1_transcript import TranscriptOptions, generate_transcript


def _write(path: Path, rows: list[dict]):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_drops_doorbell_prompt_keeps_speech(tmp_path: Path):
    p = tmp_path / "c.jsonl"
    _write(p, [
        {"role": "user", "kind": "doorbell", "content": "Your turn ended.", "ts": "2026-01-01T00:00:00+00:00"},
        {"role": "user", "kind": "prompt", "content": "huge prompt", "ts": "2026-01-01T00:00:01+00:00"},
        {"role": "user", "kind": "message", "content": "<eric (via tui)> hi", "ts": "2026-01-01T00:00:02+00:00"},
        {"role": "assistant", "kind": "speech", "content": "hello", "ts": "2026-01-01T00:00:03+00:00"},
        {"role": "assistant", "kind": "speech_repair", "content": "hello full", "ts": "2026-01-01T00:00:04+00:00"},
    ])
    r = generate_transcript(p, TranscriptOptions(strip_attribution=True))
    assert "Your turn ended" not in r.text
    assert "huge prompt" not in r.text
    assert "hello full" not in r.text
    assert "hi" in r.text
    assert "hello" in r.text


def test_memory_pack_strips_chrome_and_headers(tmp_path: Path):
    p = tmp_path / "c.jsonl"
    _write(p, [
        {
            "role": "user",
            "kind": "message",
            "content": "<eric (via tui)> hello\n[Context left 100k till autocompaction | tui | Fri Sep 11 10:00 PDT 2026]",
            "ts": "2026-09-11T17:00:00+00:00",
        },
        {
            "role": "assistant",
            "kind": "speech",
            "content": "Checking the thing.\n\n## Design\n\nThe plan is X.\n\n### Bottom line\n\nDo X.\n\n.",
            "ts": "2026-09-11T17:01:00+00:00",
        },
        {
            "role": "user",
            "kind": "message",
            "content": "<eric (via arena)> from sa",
            "ts": "2026-09-12T12:00:00+00:00",
        },
    ])
    r = generate_transcript(p, TranscriptOptions.memory_pack())
    assert "Context left" not in r.text
    assert "<eric" not in r.text
    assert "via tui" not in r.text
    assert "hello" in r.text
    assert "from sa" in r.text
    assert "U (arena):" in r.text or "(arena)" in r.text
    assert "Checking the thing" not in r.text  # doing preamble dropped
    assert "##" not in r.text  # multi-hash collapsed
    assert "# Design" in r.text  # single # marks header
    assert "Bottom line" not in r.text  # closer heading dropped
    assert "Do X" in r.text
    assert "---" in r.text  # day header
    assert r.text.count("---") >= 1


def test_drops_pre_schema_continue(tmp_path: Path):
    p = tmp_path / "c.jsonl"
    _write(p, [
        {"role": "user", "content": "[continue (id=x)] go", "ts": "2026-01-01T00:00:00+00:00"},
        {"role": "user", "content": "real question", "ts": "2026-01-01T00:00:01+00:00"},
        {"role": "assistant", "content": "answer", "ts": "2026-01-01T00:00:02+00:00"},
    ])
    r = generate_transcript(p)
    assert "[continue" not in r.text
    assert "real question" in r.text


def test_merge_consecutive(tmp_path: Path):
    p = tmp_path / "c.jsonl"
    _write(p, [
        {"role": "assistant", "kind": "speech", "content": "one"},
        {"role": "assistant", "kind": "speech", "content": "two"},
        {"role": "user", "kind": "message", "content": "q"},
    ])
    r = generate_transcript(p, TranscriptOptions(merge_consecutive=True))
    assert "one" in r.text and "two" in r.text


def test_user_index_and_expand(tmp_path: Path):
    p = tmp_path / "c.jsonl"
    rows = [
        {"role": "assistant", "kind": "speech", "content": "prev answer", "ts": "2026-09-10T12:00:00+00:00"},
        {"role": "user", "kind": "message", "content": "<eric (via tui)> about thiasai negative space", "ts": "2026-09-10T12:01:00+00:00"},
        {"role": "assistant", "kind": "speech", "content": "thiasai is X", "ts": "2026-09-10T12:02:00+00:00"},
        {"role": "user", "kind": "message", "content": "<eric (via tui)> more on evolution", "ts": "2026-09-10T13:00:00+00:00"},
        {"role": "assistant", "kind": "speech", "content": "evolved to Y", "ts": "2026-09-10T13:01:00+00:00"},
        {"role": "user", "kind": "message", "content": "<eric (via tui)> unrelated", "ts": "2026-09-11T10:00:00+00:00"},
        {"role": "assistant", "kind": "speech", "content": "ok", "ts": "2026-09-11T10:01:00+00:00"},
    ]
    _write(p, rows)
    from v1_transcript import (
        TranscriptOptions,
        build_user_index,
        expand_around,
        load_kept_entries,
        user_search,
        format_user_index,
    )
    entries, _ = load_kept_entries(p, TranscriptOptions.memory_pack(), merge=False)
    idx = build_user_index(entries)
    assert len(idx) == 3
    assert idx[0]["id"] == "u0"
    assert "thiasai" in idx[0]["snippet"]
    hits = user_search(idx, "thiasai")
    assert len(hits) == 1 and hits[0]["id"] == "u0"
    assert "content" in idx[0] and "negative space" in idx[0]["content"]
    from v1_transcript import format_user_index
    full = format_user_index(idx, full_content=True)
    assert "about thiasai negative space" in full
    long_msg = "ok. MVP looks like: " + ("share a document " * 20)
    rows2 = [
        {"role": "user", "kind": "message", "content": f"<eric (via tui)> {long_msg}",
         "ts": "2026-09-07T17:19:21+00:00"},
    ]
    p2 = tmp_path / "c2.jsonl"
    _write(p2, rows2)
    e2, _ = load_kept_entries(p2, TranscriptOptions.memory_pack(), merge=False)
    i2 = build_user_index(e2)
    assert "share a document" in i2[0]["content"]
    full2 = format_user_index(i2, full_content=True)
    assert "share a document share a document" in full2
    assert full2.count("share a document") >= 15  # not truncated
    snip = format_user_index(i2, full_content=False)
    assert "…" in snip
    exp = expand_around(entries, handle="u0", before=0, after=1)
    assert "thiasai negative space" in exp.text
    assert "thiasai is X" in exp.text
    assert "more on evolution" in exp.text
    assert "evolved to Y" in exp.text
    assert "unrelated" not in exp.text
    # ts expand
    exp2 = expand_around(entries, ts=hits[0]["ts"], before=0, after=0)
    assert "thiasai is X" in exp2.text
