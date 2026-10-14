"""IRC/background envelopes keep the real speaker on the bubble."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tui"))
from chat_widgets import make_user_message, speaker_from_chunk, operator_body


def test_background_squiggy_nick():
    raw = "[background] Squiggy in #standup (reply_via=irc outbox): [IRC #standup from Squiggy]\ngot it"
    assert speaker_from_chunk(raw) == "Squiggy"
    um = make_user_message(raw)
    assert um._nick.startswith("Squiggy")
    assert "got it" in um.user_text
    assert "background" not in um.user_text.lower()


def test_eric_tui_stays_eric():
    raw = "<eric (via tui)> hello"
    assert speaker_from_chunk(raw) == "eric"
    assert make_user_message(raw)._nick.startswith("eric")
