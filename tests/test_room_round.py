"""Round-robin: last noted stops; last speech wraps; mid noted advances."""


class _Batch:
    def __init__(self):
        self.added = []

    def add(self, name, msg):
        self.added.append((name, msg["text"] if isinstance(msg, dict) else msg))


def setup_module():
    import irc_adapter as ia
    ia._room_turns.clear()


def test_start_first_seat():
    import irc_adapter as ia
    ia._room_turns.clear()
    first = ia.start_room_round(
        "#gen4", ["Holt", "Squiggy"], "eric_tui", "hello", ["Holt", "Squiggy"]
    )
    assert first == "Holt"
    assert ia._floor_speaker("#Gen4") == "Holt"


def test_last_noted_stops_no_wrap():
    import irc_adapter as ia
    ia._room_turns.clear()
    ia.start_room_round("#gen4", ["Holt", "Squiggy"], "eric_tui", "hello", ["Holt", "Squiggy"])
    b = _Batch()
    assert ia.on_member_speech("#gen4", "Holt", "Holt", "hi", noted=False, batcher=b) == "next"
    assert b.added[-1][0] == "Squiggy"
    b2 = _Batch()
    assert ia.on_member_speech("#gen4", "Squiggy", "Squiggy", "noted", noted=True, batcher=b2) == "stop"
    assert b2.added == []
    assert ia._floor_speaker("#gen4") is None


def test_last_speech_wraps_to_first():
    import irc_adapter as ia
    ia._room_turns.clear()
    ia.start_room_round("#gen4", ["Holt", "Squiggy"], "eric_tui", "hello", ["Holt", "Squiggy"])
    b = _Batch()
    ia.on_member_speech("#gen4", "Holt", "Holt", "from holt", noted=False, batcher=b)
    b2 = _Batch()
    assert ia.on_member_speech("#gen4", "Squiggy", "Squiggy", "from squiggy", noted=False, batcher=b2) == "wrap"
    assert b2.added[-1][0] == "Holt"
    assert "from squiggy" in b2.added[-1][1]
    assert ia._floor_speaker("#gen4") == "Holt"


def test_non_seat_held():
    import irc_adapter as ia
    ia._room_turns.clear()
    ia.start_room_round("#gen4", ["Holt", "Squiggy"], "eric_tui", "hello", ["Holt", "Squiggy"])
    b = _Batch()
    assert ia.on_member_speech("#gen4", "Squiggy", "Squiggy", "nope", noted=False, batcher=b) == "held"
    assert b.added == []
    assert ia._floor_speaker("#gen4") == "Holt"
