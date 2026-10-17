"""Bare delay 0 without text gets a how-to on the next continue."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
from delay_cmd import (
    DELAY_ZERO_FAILSAFE_TEXT,
    delay_continue_text,
    failsafe_if_bare_delay_zero,
)


def test_bare_delay_zero_gets_failsafe():
    t = delay_continue_text(0, None)
    assert t == DELAY_ZERO_FAILSAFE_TEXT
    assert "until_event" in t
    assert "text" in t
    assert delay_continue_text(0, "") == DELAY_ZERO_FAILSAFE_TEXT
    assert delay_continue_text("0", "  ") == DELAY_ZERO_FAILSAFE_TEXT


def test_agent_text_wins():
    assert delay_continue_text(0, "Continue: pack leftover 7") == (
        "Continue: pack leftover 7"
    )


def test_nonzero_and_until_event_stay_empty():
    assert delay_continue_text(600, None) is None
    assert delay_continue_text("until_event", None) is None
    assert delay_continue_text(90, "") is None


def test_queue_failsafe_when_implicit_delay_zero():
    assert (
        failsafe_if_bare_delay_zero(
            None, until_event=False, next_turn_delay=0
        )
        == DELAY_ZERO_FAILSAFE_TEXT
    )
    assert (
        failsafe_if_bare_delay_zero(
            "keep going", until_event=False, next_turn_delay=0
        )
        == "keep going"
    )
    assert (
        failsafe_if_bare_delay_zero(
            None, until_event=True, next_turn_delay=0
        )
        is None
    )
    assert (
        failsafe_if_bare_delay_zero(
            None, until_event=False, next_turn_delay=600
        )
        is None
    )
