"""Injected skill documents must not be painted verbatim into the TUI.

Claude Code delivers a skill's whole document as a user-role turn. One landed in
hot.jsonl at 91,192 chars and the TUI rendered all of it, which from Eric's side
looked like the agent reciting documentation unprompted:

    "holy shit what just happened? it looked like you recited a wall of
     documentation. this a tui problem?"

It was not a TUI problem. `native` was already spared -- _elide_binary's generic
string rule replaced it with "[data: 91192 chars elided]" -- but `body.text`
carried the full document, and body is what the TUI reads.

The fold is deliberately two-tier: a skill document is recognisable by its first
line, so it folds at 8k; anything else user-role folds only past 32k, so a long
paste from Eric still arrives intact.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

from stream_adapters.claude import (  # noqa: E402
    FOLD_SKILL_DOC_OVER_CHARS,
    FOLD_USER_TEXT_OVER_CHARS,
    SKILL_DOC_PREFIX,
    _fold_injected_document,
)


def _skill_doc(body_chars=90_000, name="claude-api"):
    head = f"{SKILL_DOC_PREFIX} /tmp/claude-1000/bundled-skills/2.1.278/abc123/{name}\n\n"
    return head + ("# Building things\n" * (body_chars // 17))


def test_a_skill_document_is_folded():
    doc = _skill_doc()
    folded = _fold_injected_document(doc)

    assert len(folded) < len(doc)
    assert len(folded) < 2000


def test_the_fold_names_the_skill():
    """The one part worth seeing is which skill just loaded."""
    folded = _fold_injected_document(_skill_doc(name="claude-api"))
    assert "skill document: claude-api" in folded


def test_the_fold_reports_what_it_dropped():
    doc = _skill_doc()
    folded = _fold_injected_document(doc)

    assert str(len(doc)) in folded
    assert "elided" in folded


def test_the_opening_of_the_document_survives():
    folded = _fold_injected_document(_skill_doc())
    assert folded.startswith(SKILL_DOC_PREFIX)


def test_a_short_skill_document_is_left_alone():
    """Folding is for walls of text, not for anything bearing the prefix."""
    short = f"{SKILL_DOC_PREFIX} /tmp/x/tiny\n\nDo the thing."
    assert _fold_injected_document(short) == short


def test_erics_ordinary_message_is_untouched():
    msg = "hey astro. i think we're good. trip is taking some more."
    assert _fold_injected_document(msg) == msg


def test_a_long_human_paste_is_untouched():
    """Between the two thresholds: too big for a skill doc rule, still a message."""
    paste = "x" * (FOLD_SKILL_DOC_OVER_CHARS + 1000)
    assert len(paste) < FOLD_USER_TEXT_OVER_CHARS
    assert _fold_injected_document(paste) == paste


def test_a_giant_non_skill_injection_still_folds():
    """No human types 32k, so past that it is a document whatever it is."""
    blob = "y" * (FOLD_USER_TEXT_OVER_CHARS + 1)
    folded = _fold_injected_document(blob)

    assert len(folded) < len(blob)
    assert "injected document" in folded


def test_leading_whitespace_does_not_hide_a_skill_document():
    doc = "\n  " + _skill_doc()
    assert "skill document:" in _fold_injected_document(doc)


def test_non_strings_pass_through():
    assert _fold_injected_document(None) is None


def test_the_thresholds_are_ordered():
    assert FOLD_SKILL_DOC_OVER_CHARS < FOLD_USER_TEXT_OVER_CHARS
