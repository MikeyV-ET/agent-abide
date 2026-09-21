"""A session limit is detected from what the CLI SAYS, never from what the model says.

Seen live 2026-09-21: the detector regex-scanned the agent's own speech for
"usage limit" / "session limit". At 01:42 Astro wrote "I was parked on a usage
limit" while explaining the previous night — and was parked for an hour, twice,
while not limited at all. Any agent that discusses limits gets parked.

Ground truth from Astro's transcript: real limits arrive as CLI-authored
assistant messages — model "<synthetic>", isApiErrorMessage true,
error "rate_limit" — with text like
"You've hit your session limit · resets 8:40pm (America/Los_Angeles)".
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

from claude_backend import ClaudeBackend  # noqa: E402

REAL_LIMIT = "You've hit your session limit · resets 8:40pm (America/Los_Angeles)"
MY_0142_SENTENCE = (
    "Trip-G has an explanation for the gap I couldn't account for: I was "
    "**parked on a usage limit**, not dropping turns."
)


def _assistant(text, model="claude-opus-5", **extra):
    frame = {"type": "assistant", "message": {"model": model, "content": [{"type": "text", "text": text}]}}
    frame.update(extra)
    return frame


def _feed(be, frame):
    speech = []
    be._process_frame(frame, speech, [], None, None, None)
    return speech


def test_model_speech_about_limits_does_not_park():
    """The exact 01:42 false positive."""
    be = ClaudeBackend()
    _feed(be, _assistant(MY_0142_SENTENCE))
    assert not be.session_limited


def test_model_quoting_the_real_limit_text_does_not_park():
    """Even quoting the CLI's own wording is still the model talking."""
    be = ClaudeBackend()
    _feed(be, _assistant(f'The CLI said: "{REAL_LIMIT}"'))
    assert not be.session_limited


def test_cli_synthetic_limit_message_parks():
    be = ClaudeBackend()
    _feed(be, _assistant(REAL_LIMIT, model="<synthetic>", isApiErrorMessage=True, error="rate_limit"))
    assert be.session_limited


def test_cli_signal_is_enough_without_the_synthetic_model_tag():
    be = ClaudeBackend()
    _feed(be, _assistant(REAL_LIMIT, isApiErrorMessage=True, error="rate_limit"))
    assert be.session_limited


def test_normal_result_frame_mentioning_limits_does_not_park():
    """On a normal turn frame['result'] IS the model's final text."""
    info = ClaudeBackend._limit_info_from_result(
        {"type": "result", "is_error": False, "result": MY_0142_SENTENCE}
    )
    assert info is None


def test_error_result_frame_with_limit_text_parks():
    info = ClaudeBackend._limit_info_from_result(
        {"type": "result", "is_error": True, "result": REAL_LIMIT}
    )
    assert info is not None and info.detected


def test_speech_is_still_collected_for_display():
    be = ClaudeBackend()
    assert _feed(be, _assistant(MY_0142_SENTENCE)) == [MY_0142_SENTENCE]
