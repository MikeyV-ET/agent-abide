"""A park ends at the reset, and nothing else ends it.

Astro, 2026-09-22, measured on a live limit:

    14:04     agent queues {"action":"delay","seconds":600}
    14:05:10  session limit hit; park written, reset_unix = 18:50
    14:15:13  "session_limit cleared -- back online", T2 = T1 + 603s
    14:15:22  next send hits the limit again; re-park, 9s later
    18:50:00  correct wake, T2 == parsed reset

The 600s delay was queued a minute BEFORE the limit and outlived the start of
the park. Two defects combined: the park hold stood aside for a delay that was
already scheduled (asdaaas), and the delay's expiry then cleared the park with
no check that the reset had passed (turn_engine).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

import session_limit  # noqa: E402
from session_limit import (  # noqa: E402
    count_queued_inputs,
    park_remaining_s,
    should_hold_park,
)


def _park(tmp_path, reset_in_s):
    adir = tmp_path
    (adir / "doorbells").mkdir(exist_ok=True)
    now = time.time()
    with open(session_limit.park_path(adir), "w") as f:
        json.dump({
            "status": "session_limited",
            "reset_unix": now + reset_in_s,
            "parked_at": now,
            "parked_at_iso": "2026-09-22T21:05:10.021492+00:00",
        }, f)
    return adir


# --- D1: only the reset clears a park -------------------------------------


def test_a_live_park_still_holds_when_an_unrelated_delay_expires(tmp_path):
    """The guard turn_engine now consults before waking on delay expiry."""
    adir = _park(tmp_path, reset_in_s=4 * 3600)
    assert should_hold_park(adir) is True


def test_a_park_past_its_reset_does_not_hold(tmp_path):
    adir = _park(tmp_path, reset_in_s=-60)
    assert should_hold_park(adir) is False


def test_remaining_time_is_measured_from_the_reset_not_the_delay(tmp_path):
    adir = _park(tmp_path, reset_in_s=4 * 3600)
    rem = park_remaining_s(adir)
    assert rem is not None and rem > 3 * 3600


# --- D2: a park caps the next delay, it never defers to one ---------------


def _chunk_for(holding, remaining_s, next_turn_delay, delay_until_event):
    """The decision asdaaas.py makes each idle tick, isolated.

    Mirrors the patched block; the pre-fix version was gated on
    `next_turn_delay <= 0 and not delay_until_event` and returned the caller's
    delay untouched whenever one was already pending.
    """
    if not holding:
        return next_turn_delay, delay_until_event
    rem = float(remaining_s or 0)
    if rem > 0:
        chunk = min(rem, 600.0)
        if delay_until_event or next_turn_delay <= 0 or next_turn_delay > chunk:
            next_turn_delay = chunk
        delay_until_event = False
    return next_turn_delay, delay_until_event


def test_a_pending_agent_delay_no_longer_suppresses_the_hold():
    """The live case: 600s already queued, 4.5h of park left."""
    delay, until_event = _chunk_for(True, 4 * 3600, next_turn_delay=600, delay_until_event=False)
    assert delay == 600.0
    assert until_event is False


def test_a_delay_longer_than_the_chunk_is_capped():
    delay, _ = _chunk_for(True, 4 * 3600, next_turn_delay=5000, delay_until_event=False)
    assert delay == 600.0


def test_a_delay_shorter_than_the_chunk_is_left_alone():
    """Waking early costs one cheap tick; reconcile just re-chunks."""
    delay, _ = _chunk_for(True, 4 * 3600, next_turn_delay=30, delay_until_event=False)
    assert delay == 30


def test_until_event_does_not_outlast_the_park():
    """An idle agent must not sleep through its own reset waiting for input."""
    delay, until_event = _chunk_for(True, 4 * 3600, next_turn_delay=0, delay_until_event=True)
    assert delay == 600.0
    assert until_event is False


def test_the_last_chunk_is_the_remainder_not_a_full_chunk():
    delay, _ = _chunk_for(True, 90, next_turn_delay=0, delay_until_event=False)
    assert delay == 90.0


def test_nothing_is_touched_when_not_holding():
    delay, until_event = _chunk_for(False, 0, next_turn_delay=600, delay_until_event=True)
    assert (delay, until_event) == (600, True)


# --- D3: the queued-input count is about input, not about us --------------


def _bell(adir, name):
    d = adir / "doorbells"
    d.mkdir(exist_ok=True)
    with open(d / name, "w") as f:
        json.dump({"text": "x"}, f)


def test_our_own_wake_notice_is_not_counted_as_queued_input(tmp_path):
    """The live false positive: "Queued while parked: 1 item(s)" was the
    previous park's unacked wake notice talking to itself."""
    _bell(tmp_path, "wake_sesslim_66t12b6s.json")
    counts = count_queued_inputs(tmp_path)
    assert counts["doorbells"] == 0
    assert counts["total"] == 0


def test_continues_are_still_not_counted(tmp_path):
    _bell(tmp_path, "cont_abc.json")
    assert count_queued_inputs(tmp_path)["total"] == 0


def test_a_real_message_is_still_counted(tmp_path):
    """The whole point of the count -- this must not regress to zero."""
    _bell(tmp_path, "bell_w7gvh2xv.json")
    counts = count_queued_inputs(tmp_path)
    assert counts["doorbells"] == 1
    assert counts["total"] == 1


def test_a_real_message_is_found_alongside_our_own_notices(tmp_path):
    _bell(tmp_path, "wake_sesslim_66t12b6s.json")
    _bell(tmp_path, "cont_abc.json")
    _bell(tmp_path, "bell_real.json")
    assert count_queued_inputs(tmp_path)["doorbells"] == 1


# --- the park caps the delay, it never defers to one -----------------------
#
# The 14:15 false wake needed both halves. turn_engine cleared the park when a
# delay expired (covered by should_hold_park above); the main loop let that
# delay run in the first place, because the chunk was applied only when the
# agent had nothing queued. These cover the second half, which the rest of this
# file did not reach -- verified by running it against the unfixed tree, where
# only the count_queued_inputs tests failed.

from session_limit import PARK_DELAY_CHUNK_S, park_delay_chunk  # noqa: E402


def test_an_already_queued_delay_does_not_outrun_the_park():
    """The live case: 600s queued at 14:04, park until 18:50 (~16,500s left)."""
    assert park_delay_chunk(16_500, 600, False) == PARK_DELAY_CHUNK_S


def test_a_long_queued_delay_is_capped_to_the_chunk():
    assert park_delay_chunk(16_500, 7_200, False) == PARK_DELAY_CHUNK_S


def test_until_event_is_overridden_by_the_park():
    """delay_until_event used to suppress the chunk entirely."""
    assert park_delay_chunk(16_500, 0, True) == PARK_DELAY_CHUNK_S


def test_no_delay_of_our_own_still_gets_the_chunk():
    assert park_delay_chunk(16_500, 0, False) == PARK_DELAY_CHUNK_S


def test_a_shorter_delay_is_left_alone():
    """A park must not make the loop less responsive than it already was."""
    assert park_delay_chunk(16_500, 60, False) == 60


def test_the_last_stretch_of_a_park_is_not_padded_out():
    """Near the reset the chunk is the remainder, not a full 600s."""
    assert park_delay_chunk(45, 600, False) == 45
    assert park_delay_chunk(45, 0, True) == 45


def test_an_expired_park_does_not_touch_the_delay():
    assert park_delay_chunk(0, 600, False) is None
    assert park_delay_chunk(-5, 600, False) is None
