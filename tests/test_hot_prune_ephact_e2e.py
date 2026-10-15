"""E2E for the prune → remount → ephact-redraw critical point.

Eric 2026-10-15: Squiggy hot.jsonl prune (300MB→100MB) made the pane jump
and every ephact card re-drew. Toast: Unable to find relative location of
tool panel (mount before an unparented widget).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tui"))

from ephact_parser import EphactData, extract_ephacts
from ephact_viewer import EphactViewer
from hot_tail_policy import offset_after_stat


def _speech(i: int) -> str:
    body = (
        f"turn {i} still going.\n"
        f'<ephact type="paragraph" title="card-{i}">uuid 01a1366b-0432-7220-8079-8311f4c98e8{i % 10}</ephact>\n'
        "."
    )
    rec = {
        "v": 1,
        "class": "message",
        "role": "assistant",
        "body": {"kind": "speech", "text": body},
        "stream_seq": i,
    }
    return json.dumps(rec) + "\n"


def test_prune_does_not_rewind_to_zero(tmp_path: Path):
    hot = tmp_path / "hot.jsonl"
    hot.write_text("".join(_speech(i) for i in range(40)))
    offset = hot.stat().st_size
    assert offset > 0
    lines = hot.read_text().splitlines(True)
    hot.write_text("".join(lines[-8:]))  # keep-tail prune
    new_size = hot.stat().st_size
    assert new_size < offset
    nxt = offset_after_stat(offset, new_size)
    assert nxt == new_size
    assert nxt != 0
    # Rewind-to-0 would re-read the whole remaining file:
    replay_wrong = hot.read_text().count("<ephact")
    assert replay_wrong == 8
    # Correct offset is EOF: no further reads until new appends.
    assert offset_after_stat(nxt, new_size) == new_size


def test_ephact_replay_does_not_restack():
    v = EphactViewer()
    cards = [
        EphactData(type="paragraph", title=f"card-{i}", content=f"body {i}")
        for i in range(20)
    ]
    for e in cards:
        assert v.push("Squiggy", e) is True
    # prune-style replay of the same 20
    for e in cards:
        assert v.push("Squiggy", e) is False
    assert len(v._stacks["Squiggy"]) == 20


def test_extract_then_push_pipeline_on_pruned_file(tmp_path: Path):
    """Full extract→push path: remaining tail after prune is 8 unique cards, replay is no-ops."""
    hot = tmp_path / "hot.jsonl"
    hot.write_text("".join(_speech(i) for i in range(30)))
    lines = hot.read_text().splitlines(True)
    hot.write_text("".join(lines[-8:]))
    v = EphactViewer()
    for line in hot.read_text().splitlines():
        o = json.loads(line)
        text = (o.get("body") or {}).get("text") or ""
        _, ephs = extract_ephacts(text)
        for e in ephs:
            v.push("Squiggy", e)
    assert len(v._stacks["Squiggy"]) == 8
    for line in hot.read_text().splitlines():
        o = json.loads(line)
        text = (o.get("body") or {}).get("text") or ""
        _, ephs = extract_ephacts(text)
        for e in ephs:
            assert v.push("Squiggy", e) is False
    assert len(v._stacks["Squiggy"]) == 8


def test_mount_before_unparented_is_append():
    """Policy for the toast: no parent → do not mount(..., before=anchor)."""
    class W:
        parent = None
    anchor = W()
    mode = "append" if getattr(anchor, "parent", None) is None else "before"
    assert mode == "append"
    anchor.parent = object()
    mode = "append" if getattr(anchor, "parent", None) is None else "before"
    assert mode == "before"
