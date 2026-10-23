"""Round-robin per Eric 2026-10-23:

A speak → B noted → C noted → stop
A speak → B speak → C noted → wrap A
A noted → B speak → C noted → wrap A
A noted → B speak → C noted → A noted → stop
"""


class _Batch:
    def __init__(self):
        self.added = []

    def add(self, name, msg):
        self.added.append((name, msg["text"] if isinstance(msg, dict) else msg))


def _round3():
    import irc_adapter as ia
    ia._room_turns.clear()
    ia.start_room_round(
        "#gen4",
        ["Holt", "Squiggy", "Wend"],
        "eric_tui",
        "hello",
        ["Holt", "Squiggy", "Wend"],
    )
    ia._room_turns["#gen4"]["order"] = ["Holt", "Squiggy", "Wend"]
    ia._room_turns["#gen4"]["i"] = 0
    return ia


def _go(ia, name, *, noted, text="x"):
    b = _Batch()
    st = ia.on_member_speech("#gen4", name, name, "noted" if noted else text, noted=noted, batcher=b)
    return st, b


def test_speak_then_tail_notes_stops():
    ia = _round3()
    assert _go(ia, "Holt", noted=False, text="hi")[0] == "next"
    assert _go(ia, "Squiggy", noted=True)[0] == "next"
    st, b = _go(ia, "Wend", noted=True)
    assert st == "stop"
    assert b.added == []
    assert ia._floor_speaker("#gen4") is None


def test_speak_speak_noted_wraps_to_a():
    ia = _round3()
    _go(ia, "Holt", noted=False, text="a")
    _go(ia, "Squiggy", noted=False, text="b")
    st, b = _go(ia, "Wend", noted=True)
    assert st == "wrap"
    assert b.added[-1][0] == "Holt"
    assert "b" in b.added[-1][1]
    assert ia._floor_speaker("#gen4") == "Holt"


def test_noted_speak_noted_wraps_to_a():
    ia = _round3()
    _go(ia, "Holt", noted=True)
    _go(ia, "Squiggy", noted=False, text="b")
    st, b = _go(ia, "Wend", noted=True)
    assert st == "wrap"
    assert b.added[-1][0] == "Holt"


def test_wrap_then_a_notes_stops():
    ia = _round3()
    _go(ia, "Holt", noted=True)
    _go(ia, "Squiggy", noted=False, text="b")
    _go(ia, "Wend", noted=True)
    st, b = _go(ia, "Holt", noted=True)
    assert st == "stop"
    assert ia._floor_speaker("#gen4") is None


def test_non_seat_held():
    ia = _round3()
    st, b = _go(ia, "Wend", noted=False, text="nope")
    assert st == "held"
    assert b.added == []
    assert ia._floor_speaker("#gen4") == "Holt"
