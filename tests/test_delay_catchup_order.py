"""Delay catch-up must not claim delay is after final speech when hot has speech after."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import line_to_tui_event, is_dialogue_speech  # noqa: E402


def _speech_after_latest_delay(lines: list[str]) -> tuple[bool, int]:
    """Mirror TUI scan: index of latest delay-ish tool line + dialogue after?"""
    latest_i = -1
    for i, line in enumerate(lines):
        if "delay" not in line:
            continue
        if "tool_call" not in line and "tool_use" not in line:
            continue
        if "commands/cmd_" not in line and "action" not in line:
            continue
        if '"action"' in line and "delay" in line:
            latest_i = i
        elif "action" in line and "delay" in line:
            latest_i = i
    if latest_i < 0:
        return False, -1
    for line in lines[latest_i + 1 :]:
        ev = line_to_tui_event(line, "hot")
        if ev is not None and is_dialogue_speech(ev):
            return True, latest_i
    return False, latest_i


def test_squiggy_hot_delay_has_speech_after():
    hot = Path("/home/eric/agents/LeviSmith/Squiggy/asdaaas/history/hot.jsonl")
    if not hot.exists():
        return
    size = hot.stat().st_size
    with open(hot, "rb") as f:
        f.seek(max(0, size - 2_000_000))
        if f.tell() > 0:
            f.readline()
        data = f.read().decode("utf-8", errors="replace")
    lines = data.splitlines()
    speech_after, idx = _speech_after_latest_delay(lines)
    assert idx >= 0, "expected a delay tool near tip"
    assert speech_after is True, (
        "Squiggy 09:52 pattern: delay then final agent text — catch-up must "
        "not remount delay at bottom"
    )


def test_synthetic_delay_then_speech():
    delay_line = json.dumps(
        {
            "format": "aa.stream",
            "class": "tool",
            "body": {
                "kind": "tool_call",
                "name": "run_terminal_command",
                "text": 'cat > commands/cmd_x.json << EOF\n{"action":"delay","seconds":600}\nEOF',
            },
            "native": {
                "schema": "grok.session_update.v1",
                "event": {
                    "params": {
                        "update": {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "call-delay",
                            "title": "run_terminal_command",
                            "rawInput": {
                                "command": 'echo \'{"action":"delay","seconds":600}\' > commands/cmd_x.json'
                            },
                        }
                    }
                },
            },
        }
    )
    speech_line = json.dumps(
        {
            "format": "aa.stream",
            "class": "message",
            "role": "assistant",
            "body": {"kind": "text", "text": "r4 rewrites are on disk. standing by."},
        }
    )
    ok, _ = _speech_after_latest_delay([delay_line, speech_line])
    assert ok is True


def test_synthetic_speech_then_delay_no_speech_after():
    speech_line = json.dumps(
        {
            "format": "aa.stream",
            "class": "message",
            "role": "assistant",
            "body": {"kind": "text", "text": "done talking."},
        }
    )
    delay_line = json.dumps(
        {
            "format": "aa.stream",
            "class": "tool",
            "body": {
                "kind": "tool_call",
                "name": "run_terminal_command",
                "text": 'cat > asdaaas/commands/cmd_x.json << EOF\n{"action": "delay", "seconds": 60}\nEOF',
            },
            "native": {
                "event": {
                    "params": {
                        "update": {
                            "sessionUpdate": "tool_call",
                            "rawInput": {
                                "command": "cat > asdaaas/commands/cmd_x.json << EOF\n{\"action\": \"delay\", \"seconds\": 60}\nEOF"
                            },
                        }
                    }
                }
            },
        }
    )
    ok, idx = _speech_after_latest_delay([speech_line, delay_line])
    assert idx >= 0
    assert ok is False
