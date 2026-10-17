"""Delay-command helpers.

Bare ``delay 0`` with no ``text`` is how agents spin without knowing they can
park or leave a note to themselves. Fill that hole with a short how-to on the
next continue.
"""

DELAY_ZERO_FAILSAFE_TEXT = (
    "delay 0 with no text — next turn is immediate. "
    'Park: {"action":"delay","seconds":"until_event"}. '
    'Wait N seconds: {"action":"delay","seconds":N}. '
    'Steer yourself with "text": "what to do next". '
    "If you mean to keep working, add that text so you are not spinning blind."
)


def delay_continue_text(seconds, text):
    """Continue payload for a delay command.

    Agent-supplied ``text`` always wins. Bare delay 0 (no text) gets
    DELAY_ZERO_FAILSAFE_TEXT. until_event and N>0 with no text stay None
    (default continue message).
    """
    raw = (text or "").strip() or None
    if raw:
        return raw
    if seconds == "until_event":
        return None
    try:
        if float(seconds) == 0.0:
            return DELAY_ZERO_FAILSAFE_TEXT
    except (TypeError, ValueError):
        pass
    return None


def failsafe_if_bare_delay_zero(delay_text, *, until_event, next_turn_delay):
    """If a continue is about to fire at delay 0 with no text, attach the how-to."""
    if delay_text:
        return delay_text
    if until_event:
        return None
    try:
        if float(next_turn_delay or 0) == 0.0:
            return DELAY_ZERO_FAILSAFE_TEXT
    except (TypeError, ValueError):
        pass
    return delay_text
