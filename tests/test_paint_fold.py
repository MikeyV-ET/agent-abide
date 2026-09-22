"""Paint fold: hot events → ChatState paint units with display policy."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tui"))
sys.path.insert(0, str(ROOT / "core"))

from chat_model import ChatState, SpeechItem, ToolItem, SystemItem  # noqa: E402
from paint_fold import (  # noqa: E402
    DisplayPolicy,
    fold_hot_lines,
    fold_hot_file_tail,
    paint_units,
    count_meaningful_paint,
    enough_for_tip,
    policy_for_item,
    event_from_hot_line,
)


def _aa(seq, cls, role, phase, kind, **body):
    return json.dumps(
        {
            "format": "aa.stream",
            "v": 1,
            "stream_seq": seq,
            "class": cls,
            "role": role,
            "phase": phase,
            "body": {"kind": kind, **body},
            "backend": "grok",
        }
    )


def _tool_start(seq, tid, name="run_terminal_command"):
    return json.dumps(
        {
            "format": "aa.stream",
            "stream_seq": seq,
            "class": "tool",
            "role": "assistant",
            "phase": "start",
            "body": {"kind": "tool_call", "id": tid, "name": name, "status": "started"},
            "native": {
                "event": {
                    "params": {
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": tid,
                            "title": name,
                            "status": "pending",
                        }
                    }
                }
            },
            "backend": "grok",
        }
    )


def _tool_result(seq, tid, text="ok"):
    return json.dumps(
        {
            "format": "aa.stream",
            "stream_seq": seq,
            "class": "tool",
            "role": "tool",
            "phase": "end",
            "body": {"kind": "tool_result", "tool_id": tid, "content": text},
            "native": {
                "event": {
                    "params": {
                        "update": {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": tid,
                            "status": "completed",
                            "content": [
                                {
                                    "type": "content",
                                    "content": {"type": "text", "text": text},
                                }
                            ],
                        }
                    }
                }
            },
            "backend": "grok",
        }
    )


def _retry(seq, attempt=1):
    return json.dumps(
        {
            "format": "aa.stream",
            "stream_seq": seq,
            "class": "unknown",
            "body": {"kind": "raw_only"},
            "native": {
                "event": {
                    "params": {
                        "update": {
                            "sessionUpdate": "retry_state",
                            "type": "retrying",
                            "attempt": attempt,
                            "max_retries": 15,
                            "reason": "API error 500 unavailable",
                        }
                    }
                }
            },
            "backend": "grok",
        }
    )


def test_policies():
    assert policy_for_item(SpeechItem(text="hi", kind="user")) is DisplayPolicy.FULL
    assert policy_for_item(ToolItem(tool_id="t1")) is DisplayPolicy.SNIPPET


def test_fold_user_agent_tool_merge():
    lines = [
        _aa(1, "message", "user", "delta", "text_delta", text="hello from eric"),
        _aa(2, "message", "assistant", "delta", "text_delta", text="checking…"),
        _tool_start(3, "call-abc"),
        _tool_result(4, "call-abc", "stdout here " * 50),
        _aa(5, "message", "assistant", "delta", "text_delta", text="done."),
        json.dumps(
            {
                "format": "aa.stream",
                "stream_seq": 6,
                "class": "meta",
                "phase": "end",
                "body": {"kind": "meta", "label": "turn_completed"},
                "native": {
                    "event": {
                        "params": {"update": {"sessionUpdate": "turn_completed"}}
                    }
                },
            }
        ),
    ]
    st = ChatState()
    r = fold_hot_lines(st, lines)
    assert r.events_dropped >= 1  # turn_completed
    units = paint_units(st)
    kinds = [type(u).__name__ for u, _ in units]
    # user speech, agent speech, tool, agent speech (turn marks may appear)
    assert any(isinstance(u, SpeechItem) and u.kind == "user" for u, _ in units)
    assert any(isinstance(u, SpeechItem) and u.kind == "agent" for u, _ in units)
    tools = [u for u, _ in units if isinstance(u, ToolItem)]
    assert len(tools) == 1
    assert tools[0].tool_id == "call-abc"
    assert tools[0].status in ("completed", "running", "pending") or tools[0].output
    # tool policy snippet
    assert all(p is DisplayPolicy.SNIPPET for u, p in units if isinstance(u, ToolItem))
    assert all(p is DisplayPolicy.FULL for u, p in units if isinstance(u, SpeechItem))


def test_retry_becomes_banner():
    st = ChatState()
    fold_hot_lines(st, [_retry(1, 3), _retry(2, 4)])
    sys_items = [u for u, p in paint_units(st) if isinstance(u, SystemItem)]
    assert len(sys_items) >= 2
    assert "Retry 3/15" in sys_items[0].text or "Retry" in sys_items[0].text
    assert all(p is DisplayPolicy.BANNER for u, p in paint_units(st) if isinstance(u, SystemItem))


def test_enough_for_tip():
    st = ChatState()
    lines = []
    for i in range(30):
        lines.append(
            _aa(i * 2, "message", "user", "delta", "text_delta", text=f"u{i}")
        )
        lines.append(
            _aa(i * 2 + 1, "message", "assistant", "delta", "text_delta", text=f"a{i}")
        )
    fold_hot_lines(st, lines)
    assert enough_for_tip(st, min_full=12, min_total=20)
    c = count_meaningful_paint(st)
    assert c["full"] >= 12


def test_live_tripg_tail_folds():
    hot = Path("/home/eric/agents/Trip-G/asdaaas/history/hot.jsonl")
    if not hot.exists() or hot.stat().st_size < 10_000:
        return
    r = fold_hot_file_tail(hot, max_bytes=2 * 1024 * 1024)
    c = count_meaningful_paint(r.state)
    assert c["total"] >= 5, c
    # at least some speech or tools
    assert c["speech"] + c["tools"] >= 3, c
