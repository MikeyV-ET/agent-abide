"""Round-robin: last noted stops; last speech wraps; mid noted advances."""


class _Batch:
    def __init__(self):
        self.added = []

    def add(self, name, msg):
        self.added.append((name, msg["text"] if isinstance(msg, dict) else msg))


def setup_module():
    import irc_adapter as ia
    ia._room_turns.clear()


def _round():
    import irc_adapter as ia
    ia._room_turns.clear()
    first = ia.start_room_round(
        "#gen4", ["Holt", "Squiggy"], "eric_tui", "hello", ["Holt", "Squiggy"]
    )
    order = ia._room_turns["#gen4"]["order"]
    return ia, first, order


def test_start_first_seat():
    ia, first, order = _round()
    assert first in ("Holt", "Squiggy")
    assert set(order) == {"Holt", "Squiggy"}
    assert ia._floor_speaker("#Gen4") == first


def test_last_noted_stops_no_wrap():
    ia, first, order = _round()
    second = order[1]
    b = _Batch()
    assert ia.on_member_speech("#gen4", first, first, "hi", noted=False, batcher=b) == "next"
    assert b.added[-1][0] == second
    b2 = _Batch()
    assert ia.on_member_speech("#gen4", second, second, "noted", noted=True, batcher=b2) == "stop"
    assert b2.added == []
    assert ia._floor_speaker("#gen4") is None


def test_last_speech_wraps_to_first():
    ia, first, order = _round()
    second = order[1]
    b = _Batch()
    ia.on_member_speech("#gen4", first, first, "from first", noted=False, batcher=b)
    b2 = _Batch()
    assert ia.on_member_speech("#gen4", second, second, "from second", noted=False, batcher=b2) == "wrap"
    assert b2.added[-1][0] == first
    assert "from second" in b2.added[-1][1]
    assert ia._floor_speaker("#gen4") == first


def test_non_seat_held():
    ia, first, order = _round()
    other = order[1]
    b = _Batch()
    assert ia.on_member_speech("#gen4", other, other, "nope", noted=False, batcher=b) == "held"
    assert b.added == []
    assert ia._floor_speaker("#gen4") == first
