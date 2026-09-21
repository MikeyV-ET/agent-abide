"""Controlled regression: lazy-load past chrome walls and fat-line stalls."""
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
    scan_older_history_events,
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


def _write_wall_hot(path: Path, *, bloat_native: bool = False, fat_tool: bool = False) -> int:
    """Synthetic hot log: early dialogue, chrome wall, optional fat tool, side-effect anchor."""
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
            # chrome-classified? no — but native-bloated real dialogue is sparse;
            # only a few pads so speech budget can reach early*
            if i < 3:
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
    if fat_tool:
        # One tool_result line LONGER than the 64KB scan window default used in tests (64k)
        # Production window is 512KB; tests pass window=64*1024 to force the stall path.
        fat_payload = "X" * (200 * 1024)
        seq += 1
        lines.append(
            {
                "format": "aa.stream",
                "stream_seq": seq,
                "class": "tool",
                "role": "assistant",
                "body": {
                    "kind": "tool_result",
                    "tool_id": "toolu_fat",
                    "text": fat_payload,
                },
                "native": {"event": {"huge": fat_payload}},
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
    # earliest = start of the side-effect line
    return blob.rfind(b"\n", 0, idx) + 1


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
    t0 = time.perf_counter()
    result = scan_older_history_events(
        str(hot), anchor_off, "hot", speech_target=25, window=64 * 1024
    )
    dt = time.perf_counter() - t0
    assert result["speech_n"] >= 25, result
    assert result["new_earliest"] < anchor_off
    assert dt < 2.0, f"too slow {dt:.2f}s"
    texts = []
    for ev in result["events"]:
        c = (ev.get("params") or {}).get("update", {}).get("content") or {}
        texts.append(c.get("text", "") if isinstance(c, dict) else "")
    assert any("early user turn" in t or "early agent reply" in t for t in texts), texts[:8]


def test_scan_with_native_bloat_is_fast(tmp_path: Path):
    hot = tmp_path / "hot.jsonl"
    anchor_off = _write_wall_hot(hot, bloat_native=True)
    t0 = time.perf_counter()
    result = scan_older_history_events(
        str(hot), anchor_off, "hot", speech_target=25, window=64 * 1024
    )
    dt = time.perf_counter() - t0
    assert result["speech_n"] >= 25, result
    assert result["new_earliest"] < anchor_off
    assert dt < 2.0, f"too slow {dt:.2f}s — would freeze scroll"
    texts = []
    for ev in result["events"]:
        c = (ev.get("params") or {}).get("update", {}).get("content") or {}
        texts.append(c.get("text", "") if isinstance(c, dict) else "")
    assert any("early" in t for t in texts), texts[:8]


def test_fat_line_longer_than_window_does_not_spin(tmp_path: Path):
    """The Astro wall bug: tool_result line > window pins cursor forever."""
    hot = tmp_path / "hot.jsonl"
    anchor_off = _write_wall_hot(hot, fat_tool=True)
    # tiny window forces the fat tool line to span the entire read window
    t0 = time.perf_counter()
    result = scan_older_history_events(
        str(hot),
        anchor_off,
        "hot",
        speech_target=25,
        window=64 * 1024,  # fat tool is ~200KB+
        max_bytes=8 * 1024 * 1024,
    )
    dt = time.perf_counter() - t0
    assert dt < 2.0, f"spin/stuck {dt:.2f}s result={result}"
    assert result["iterations"] < 500, result
    assert result["speech_n"] >= 25, result
    assert result["new_earliest"] < anchor_off
    texts = []
    for ev in result["events"]:
        c = (ev.get("params") or {}).get("update", {}).get("content") or {}
        texts.append(c.get("text", "") if isinstance(c, dict) else "")
    assert any("early" in t for t in texts), texts[:8]
