"""Interjections carry when they were SENT and when they were DELIVERED.

Eric asked for both in the interjection itself (2026-09-21). Before this, the
header had one minute-precision time taken when asdaaas formatted the message —
neither the send time nor the delivery time.

The send time comes from the inbox message's own "ts", which two writers spell
differently: the TUI writes an ISO string, adapter_api.write_to_adapter_inbox an
epoch float. Both must parse. Delivery is only claimed for the stdin path; the
BASH_ENV fallback is labelled "queued", because it lands whenever the next
bash -c happens to run, and the watcher cannot know when that is.
"""
import datetime
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

from interjection import format_message_for_interjection  # noqa: E402

CLOCK = r"\d{2}:\d{2}:\d{2}"


def _msg(ts, text="hello", sender="eric", adapter="tui"):
    return {"text": text, "from": sender, "adapter": adapter, "id": "bell_abc123", "ts": ts}


def _local_clock(epoch):
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def test_epoch_send_time_is_shown():
    sent = 1789964788.0
    out = format_message_for_interjection(_msg(sent), stage="delivered", at=sent + 4)
    # Date and zone ride on the first stamp: "sent Sun Sep 20 21:26:28 PDT, delivered 21:26:32"
    assert re.search(rf"sent [^,]*{_local_clock(sent)}", out)
    assert f"delivered {_local_clock(sent + 4)}" in out


def test_iso_send_time_from_the_tui_is_shown():
    """The TUI writes ts as an ISO string, not epoch."""
    sent = 1789964788.0
    iso = datetime.datetime.fromtimestamp(sent, datetime.timezone.utc).isoformat()
    out = format_message_for_interjection(_msg(iso), stage="delivered", at=sent + 4)
    assert re.search(rf"sent [^,]*{_local_clock(sent)}", out)


def test_both_times_have_seconds():
    """Minute precision could not show a 4-second gap."""
    out = format_message_for_interjection(_msg(time.time()), stage="delivered")
    assert re.search(rf"sent [^,]*{CLOCK}", out)
    assert re.search(rf"delivered [^,)]*{CLOCK}", out)


def test_fallback_path_says_queued_not_delivered():
    out = format_message_for_interjection(_msg(time.time()), stage="queued")
    assert "queued" in out
    assert "delivered" not in out


def test_missing_or_garbage_send_time_does_not_break_formatting():
    for bad in (None, "", "not a time", {"x": 1}):
        out = format_message_for_interjection(_msg(bad), stage="delivered")
        assert "sent " not in out
        assert re.search(rf"delivered [^,)]*{CLOCK}", out)
        assert out.endswith("hello")


def test_identity_and_text_are_preserved():
    out = format_message_for_interjection(_msg(time.time(), text="stop, wrong file"))
    assert "eric" in out and "tui" in out and "bell_abc123" in out
    assert out.endswith("stop, wrong file")


def test_localmail_keeps_its_shape():
    out = format_message_for_interjection(
        _msg(time.time(), sender="Sr", adapter="localmail"), stage="delivered"
    )
    assert out.startswith("[localmail")
    assert "from Sr" in out
