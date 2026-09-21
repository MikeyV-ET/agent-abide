"""Reset time must parse from the CLI's real wording.

Ground truth, Astro's transcript 2026-09-20/21:
  "You've hit your session limit · resets 8:40pm (America/Los_Angeles)"
  "You've hit your session limit · resets 1:40am (America/Los_Angeles)"
The old pattern needed "resets at HH:MM", so both returned None, the park fell
back to "reset unknown; retry in 3600s", and the agent was woken into a still
limited account every hour.
"""
import datetime
import os
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

from session_limit import parse_reset_unix  # noqa: E402

LA = ZoneInfo("America/Los_Angeles")


def _at(y, mo, d, h, mi, tz=LA):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


def test_real_pm_reset_later_the_same_day():
    now = _at(2026, 9, 20, 17, 36)
    got = parse_reset_unix("You've hit your session limit · resets 8:40pm (America/Los_Angeles)", now=now)
    assert got == _at(2026, 9, 20, 20, 40)


def test_real_am_reset_rolls_to_the_next_day():
    now = _at(2026, 9, 20, 21, 34)
    got = parse_reset_unix("You've hit your session limit · resets 1:40am (America/Los_Angeles)", now=now)
    assert got == _at(2026, 9, 21, 1, 40)


def test_named_zone_is_honoured_not_the_local_clock():
    now = _at(2026, 9, 20, 12, 0, tz=ZoneInfo("UTC"))
    got = parse_reset_unix("resets 3:00pm (Europe/London)", now=now)
    assert got == _at(2026, 9, 20, 15, 0, tz=ZoneInfo("Europe/London"))


def test_hour_without_minutes():
    now = _at(2026, 9, 20, 17, 0)
    got = parse_reset_unix("resets 9pm (America/Los_Angeles)", now=now)
    assert got == _at(2026, 9, 20, 21, 0)


def test_old_resets_at_form_still_parses():
    assert parse_reset_unix("limit reached, resets at 3:15pm") is not None


def test_unknown_zone_does_not_crash():
    now = _at(2026, 9, 20, 17, 0)
    got = parse_reset_unix("resets 8:40pm (Not/AZone)", now=now)
    assert got is not None  # falls back to local wall clock
