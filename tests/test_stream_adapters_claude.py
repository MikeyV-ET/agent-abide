"""Claude stream adapter + backend dispatch."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

zstd = pytest.importorskip("zstandard")

from stream_adapters.claude import (  # noqa: E402
    map_claude_event,
    wrap_claude_line,
    tail_claude_once,
    find_live_session,
    NATIVE_CLAUDE,
)
from stream_adapters import tail_once_for_backend  # noqa: E402
from aa_stream import FORMAT_V  # noqa: E402


def test_map_claude_user_text():
    obj = {
        "type": "user",
        "timestamp": "2026-09-19T12:00:00.000Z",
        "message": {"role": "user", "content": "hello there"},
    }
    c, phase, role, body = map_claude_event(obj)
    assert c == "message"
    assert phase == "full"
    assert role == "user"
    assert body["kind"] == "text"
    assert body["text"] == "hello there"


def test_map_claude_assistant_text_blocks():
    obj = {
        "type": "assistant",
        "timestamp": "2026-09-19T12:00:01.000Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "READY"}],
        },
    }
    c, phase, role, body = map_claude_event(obj)
    assert c == "message" and role == "assistant"
    assert body["text"] == "READY"


def test_map_claude_tool_use():
    obj = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Bash",
                    "input": {"command": "date"},
                }
            ]
        },
    }
    c, phase, role, body = map_claude_event(obj)
    assert c == "tool" and phase == "start"
    assert body["name"] == "Bash"
    assert body["id"] == "toolu_1"


def test_map_claude_tool_result_user():
    obj = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "Sat Sep 19",
                }
            ]
        },
    }
    c, phase, role, body = map_claude_event(obj)
    assert c == "tool" and phase == "end"
    assert body["kind"] == "tool_result"
    assert "Sat" in body["content"]


def test_wrap_spine():
    obj = {"type": "user", "message": {"content": "hi"}}
    ev = wrap_claude_line(
        obj, agent="Astro", session_id="sid", stream_seq=0, source_path="/x", offset=0
    )
    assert ev["v"] == FORMAT_V
    assert ev["backend"] == "claude"
    assert ev["native"]["schema"] == NATIVE_CLAUDE
    assert ev["body"]["text"] == "hi"


def test_tail_claude_once_tmp(tmp_path: Path):
    home = tmp_path / "Astro"
    (home / "asdaaas").mkdir(parents=True)
    src = tmp_path / "sess.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "ping"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "pong"}],
            },
        },
        {"type": "attachment", "attachment": {}},
    ]
    src.write_text("".join(json.dumps(x) + "\n" for x in lines))
    r = tail_claude_once(home, "Astro", source=src, session_id="test-sid")
    assert r["status"] == "ok"
    assert r["lines_ingested"] == 3
    hot = Path(r["hot"])
    assert hot.exists()
    evs = [json.loads(l) for l in hot.read_text().splitlines() if l.strip()]
    assert len(evs) == 3
    assert evs[0]["role"] == "user"
    assert evs[1]["role"] == "assistant"
    # second tail is no-op
    r2 = tail_claude_once(home, "Astro", source=src, session_id="test-sid")
    assert r2["lines_ingested"] == 0


def test_dispatch_backend_claude(tmp_path: Path):
    home = tmp_path / "A"
    (home / "asdaaas").mkdir(parents=True)
    src = tmp_path / "s.jsonl"
    src.write_text(json.dumps({"type": "user", "message": {"content": "x"}}) + "\n")
    r = tail_once_for_backend("claude", home, "A", source=src)
    assert r["status"] == "ok"
    assert r["lines_ingested"] == 1


# --- binary payloads must not reach hot.jsonl ------------------------------
#
# Reading an image puts its base64 into the session transcript. Unfiltered,
# that lands in hot.jsonl twice (verbatim in `native`, truncated in `body`)
# and the TUI paints it as a wall of characters. Measured on Astro's own
# stream: two lines of 1.47 MB and 893 KB, file grown to 7.4 MB.

import json as _json  # noqa: E402

from stream_adapters.claude import wrap_claude_line, map_claude_event  # noqa: E402


def _image_tool_result(nbytes=400_000):
    return {
        "type": "user",
        "timestamp": "2026-09-20T17:20:00.000Z",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_img",
                    "content": [
                        {"type": "text", "text": "Read image /tmp/shot.png"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "iVBORw0KGgo" + ("A" * nbytes),
                            },
                        },
                    ],
                }
            ]
        },
    }


def _wrapped(obj):
    return wrap_claude_line(
        obj, agent="Astro", session_id="sid", stream_seq=1,
        source_path="/tmp/sess.jsonl", offset=0,
    )


def test_image_data_is_elided_from_the_hot_event():
    event = _wrapped(_image_tool_result())
    blob = _json.dumps(event)
    assert "A" * 5000 not in blob
    assert len(blob) < 100_000  # was ~1.5 MB unfiltered


def test_elision_keeps_the_fact_that_an_image_was_there():
    """Drop the bytes, keep the meaning — a reader must still see it happened."""
    _, _, _, body = map_claude_event(_image_tool_result())
    content = body["content"]
    assert "image" in content.lower()
    assert "image/png" in content or "png" in content.lower()


def test_surrounding_text_survives_elision():
    _, _, _, body = map_claude_event(_image_tool_result())
    assert "Read image /tmp/shot.png" in body["content"]


def test_ordinary_tool_results_are_untouched():
    obj = {
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "hello world"}
        ]},
    }
    _, _, _, body = map_claude_event(obj)
    assert body["content"] == "hello world"


def test_native_copy_is_elided_too():
    """`native` carries the raw line verbatim — the bigger of the two leaks."""
    event = _wrapped(_image_tool_result())
    native_blob = _json.dumps(event.get("native"))
    assert len(native_blob) < 50_000
    assert "A" * 5000 not in native_blob
