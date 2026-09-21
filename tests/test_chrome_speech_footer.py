"""Context-left footer must not mark real user turns as chrome."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import is_chrome_speech, is_dialogue_speech  # noqa: E402


def test_footer_only_is_chrome():
    assert is_chrome_speech(
        "[Context left 248k till autocompaction | compaction available | arena | Mon Sep 21 09:35 PDT]"
    )


def test_user_turn_with_footer_is_dialogue():
    text = (
        "<eric (via tui)> just reloaded dev tui. here's the last thing that i'm seeing "
        "for you (notice what's not there again):\n"
        "   ╭─ tool call-ffe\n"
        "[Context left 248k till autocompaction | compaction available | arena | Mon Sep 21 09:35 PDT]"
    )
    assert not is_chrome_speech(text)
    ev = {
        "params": {
            "update": {
                "sessionUpdate": "user_message_chunk",
                "content": {"text": text},
            }
        }
    }
    assert is_dialogue_speech(ev)


def test_continue_still_chrome():
    assert is_chrome_speech(
        "[continue (id=x, ts=Mon Sep 21 09:25 PDT)] Your turn ended. You may continue, delay, or stand by."
    )


def test_compaction_pipe_in_body_not_automatic_chrome():
    # The old bug: any "| compaction" substring → chrome
    assert not is_chrome_speech(
        "We should talk about compaction | compaction policy later.\n"
        "[Context left 10k till autocompaction | compaction available | tui]"
    )
