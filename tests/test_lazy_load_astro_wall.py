"""Controlled regression: lazy-load past chrome walls without CPU thrash."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import (  # noqa: E402
    cheap_hot_line_speech,
    is_chrome_speech,
    line_to_tui_event,
)


def _ev(seq: int, role: str, text: str, kind: str = "text") -> dict:
    return {
        "format": "aa.stream",
        "v": 1,
        "stream_seq": seq,
        "class": "message",
        "role": role,
        "body": {"kind": kind, "text": text},
        "ts": 1700000000.0 + seq,
    }


def _write_wall_hot(path: Path, *, bloat_native: bool = False) -> int:
    lines = []
    seq = 0
    for i in range(30):
        seq += 1
        lines.append(_ev(seq, "user", f"early user turn {i}"))
        seq += 1
        lines.append(_ev(seq, "assistant", f"early agent reply {i}"))
    bloat = "B" * 80000 if bloat_native else ""
    for i in range(80):
        seq += 1
        lines.append(
            _ev(
                seq,
                "user",
                f"[continue (id=cont_wall_{i}, ts=Sun Sep 20 18:10 PDT)] "
                f"Your turn ended. You may continue, delay, or stand by.\n"
                f"[Context left 424k till autocompaction | tui | Sun Sep 20 18:10 PDT]",
            )
        )
        seq += 1
        lines.append(
            _ev(
                seq,
                "assistant",
                "You have hit your session limit · resets 8:40pm (America/Los_Angeles)",
            )
        )
        if bloat_native:
            seq += 1
            lines.append(
                {
                    "format": "aa.stream",
                    "stream_seq": seq,
                    "class": "message",
                    "role": "assistant",
                    "body": {"kind": "text", "text": f"pad dialogue {i}"},
                    "native": {"event": {"pad": bloat}},
                }
            )
    seq += 1
    lines.append(_ev(seq, "assistant", "Confirmed the side effect — writing that up now."))
    for i in range(10):
        seq += 1
        lines.append(_ev(seq, "user", f"later user {i}"))
        seq += 1
        lines.append(_ev(seq, "assistant", f"later agent {i}"))

    raw_lines = [json.dumps(x, ensure_ascii=False) + "\n" for x in lines]
    blob = "".join(raw_lines).encode()
    path.write_bytes(blob)
    target = b"Confirmed the side effect"
    idx = blob.find(target)
    assert idx > 0
    return blob.rfind(b"\n", 0, idx) + 1


def _scan_from(hot: Path, earliest: int, speech_target: int = 25):
    data = hot.read_bytes()
    speech_n = 0
    cursor = earliest
    bytes_read = 0
    max_bytes = 12 * 1024 * 1024
    new_earliest = earliest
    dialogue_texts = []
    while cursor > 0 and speech_n < speech_target and bytes_read < max_bytes:
        seek = max(0, cursor - 512 * 1024)
        ds = 0 if seek == 0 else data.find(b"\n", seek) + 1
        raw = data[ds:cursor]
        bytes_read += len(raw)
        pos = ds
        parts = raw.split(b"\n")
        batch = []
        for i, lb in enumerate(parts):
            ls = pos
            pos += len(lb) + (1 if i < len(parts) - 1 else 0)
            if lb.strip() and ls < earliest:
                batch.append((ls, lb.decode("utf-8", errors="replace")))
        for ls, line in reversed(batch):
            hit = cheap_hot_line_speech(line)
            if hit is None:
                continue
            _su, text, _role = hit
            new_earliest = ls
            if is_chrome_speech(text):
                continue
            speech_n += 1
            dialogue_texts.append(text)
            if speech_n >= speech_target:
                break
        cursor = ds
        if ds <= 0:
            new_earliest = 0
            break
    return speech_n, new_earliest, dialogue_texts, bytes_read


def test_chrome_classifier():
    assert is_chrome_speech("[continue (id=x)] Your turn ended. stand by.")
    assert is_chrome_speech("You have hit your session limit · resets 8:40pm")
    assert not is_chrome_speech("Confirmed the side effect — writing that up now.")


def test_cheap_extract_ignores_native_bloat():
    line = (
        '{"format":"aa.stream","role":"assistant","body":{"kind":"text",'
        '"text":"Confirmed the side effect — writing that up now."},'
        '"native":{"event":{"huge":"' + ("x" * 50000) + '"}}}'
    )
    hit = cheap_hot_line_speech(line)
    assert hit is not None
    assert "side effect" in hit[1]


def test_scan_from_side_effect_anchor_reaches_early_dialogue(tmp_path: Path):
    hot = tmp_path / "hot.jsonl"
    anchor_off = _write_wall_hot(hot, bloat_native=False)
    speech_n, new_earliest, texts, _ = _scan_from(hot, anchor_off)
    assert speech_n >= 25, speech_n
    assert new_earliest < anchor_off
    assert any("early user turn" in t or "early agent reply" in t for t in texts), texts[:5]


def test_scan_with_native_bloat_is_fast(tmp_path: Path):
    hot = tmp_path / "hot.jsonl"
    anchor_off = _write_wall_hot(hot, bloat_native=True)
    t0 = time.perf_counter()
    speech_n, new_earliest, texts, _ = _scan_from(hot, anchor_off)
    dt = time.perf_counter() - t0
    assert speech_n >= 25, speech_n
    assert new_earliest < anchor_off
    assert dt < 2.0, f"too slow {dt:.2f}s — would freeze scroll"
    assert any("early" in t for t in texts), texts[:5]
