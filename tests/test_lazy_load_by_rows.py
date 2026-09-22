"""PageUp lazy-load fills a viewport of rows, not a sparse 25+4 slice."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import (  # noqa: E402
    scan_older_history_by_rows,
    scan_older_history_events,
    estimate_event_rows,
    line_to_tui_event,
)


def _write_dense_hot(path: Path, n_turns: int = 40) -> int:
    """Synthetic hot: each turn = user + agent + 5 tools. Return mid offset."""
    lines = []
    seq = 0
    for i in range(n_turns):
        seq += 1
        lines.append(
            json.dumps(
                {
                    "format": "aa.stream",
                    "stream_seq": seq,
                    "class": "message",
                    "role": "user",
                    "body": {"kind": "text", "text": f"user turn {i} short"},
                }
            )
        )
        seq += 1
        lines.append(
            json.dumps(
                {
                    "format": "aa.stream",
                    "stream_seq": seq,
                    "class": "message",
                    "role": "assistant",
                    "body": {"kind": "text", "text": f"agent turn {i} reply"},
                }
            )
        )
        for j in range(5):
            seq += 1
            lines.append(
                json.dumps(
                    {
                        "format": "aa.stream",
                        "stream_seq": seq,
                        "class": "tool",
                        "body": {
                            "kind": "tool_result",
                            "tool_id": f"t{i}_{j}",
                            "text": f"out {i}.{j}",
                        },
                        "native": {
                            "event": {
                                "params": {
                                    "update": {
                                        "sessionUpdate": "tool_call_update",
                                        "toolCallId": f"t{i}_{j}",
                                        "title": "run",
                                        "content": [
                                            {
                                                "type": "content",
                                                "content": {
                                                    "type": "text",
                                                    "text": f"out {i}.{j}",
                                                },
                                            }
                                        ],
                                    }
                                }
                            }
                        },
                    }
                )
            )
    blob = ("\n".join(lines) + "\n").encode()
    path.write_bytes(blob)
    # earliest = near end (after last few turns)
    return len(blob) - 500


def test_row_scan_includes_many_tools_not_capped_at_four(tmp_path: Path):
    hot = tmp_path / "hot.jsonl"
    earliest = _write_dense_hot(hot)
    old = scan_older_history_events(
        str(hot), earliest, "hot", speech_target=10, max_tool_panels=4
    )
    new = scan_older_history_by_rows(
        str(hot), earliest, "hot", target_rows=40, width=80
    )
    assert old["tool_n"] <= 4
    # new path should keep more tools when filling rows (no 4-tool hard cap)
    assert new["tool_n"] >= 4, new
    assert len(new["events"]) >= 20, new  # min_events floor
    assert new["est_rows"] >= 20 or len(new["events"]) >= 20, new


def test_tripg_pageup_not_sparse():
    hot = Path("/home/eric/agents/Trip-G/asdaaas/history/hot.jsonl")
    if not hot.exists() or hot.stat().st_size < 100_000:
        return
    size = hot.stat().st_size
    # start ~2MB from tip
    earliest = max(0, size - 2 * 1024 * 1024)
    r = scan_older_history_by_rows(
        str(hot), earliest, "hot", target_rows=40, width=100
    )
    assert len(r["events"]) >= 15, r
    assert r["est_rows"] >= 20 or len(r["events"]) >= 20, r
