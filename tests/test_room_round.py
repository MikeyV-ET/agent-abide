"""Flag machine: speak flags everyone else; noted lowers you; next flagged in circle."""


class _Batch:
    def __init__(self):
        self.added = []

    def add(self, name, msg):
        self.added.append((name, msg["text"] if isinstance(msg, dict) else msg))


def _round(names):
    import irc_adapter as ia
    ia._room_turns.clear()
    ia.start_room_round("#gen4", names, "eric_tui", "hello", names)
    ia._room_turns["#gen4"]["order"] = list(names)
    ia._room_turns["#gen4"]["i"] = 0
    ia._room_turns["#gen4"]["flags"] = set(names)
    return ia


def _go(ia, name, *, noted, text="x"):
    b = _Batch()
    st = ia.on_member_speech(
        "#gen4", name, name, "noted" if noted else text, noted=noted, batcher=b
    )
    return st, b, set(ia._room_turns.get("#gen4", {}).get("flags", set()))


def test_four_person_eric_trace():
    # Receives: B:A1; C:A1; D:A1,C1; A:C1; B:C1. Notes never delivered.
    names = ["A", "B", "C", "D"]
    ia = _round(names)
    st, b, fl = _go(ia, "A", noted=False, text="A1")
    assert st == "next" and b.added[-1][0] == "B" and fl == {"B", "C", "D"}
    assert "A1" in b.added[-1][1] and "noted" not in b.added[-1][1]
    st, b, fl = _go(ia, "B", noted=True)
    assert st == "next" and b.added[-1][0] == "C" and fl == {"C", "D"}
    assert "A1" in b.added[-1][1] and "noted" not in b.added[-1][1]
    st, b, fl = _go(ia, "C", noted=False, text="C1")
    assert st == "next" and b.added[-1][0] == "D" and fl == {"A", "B", "D"}
    body = b.added[-1][1]
    assert "A1" in body and "C1" in body and "noted" not in body
    st, b, fl = _go(ia, "D", noted=True)
    assert st == "wrap" and b.added[-1][0] == "A" and fl == {"A", "B"}
    assert "C1" in b.added[-1][1] and "A1" not in b.added[-1][1]
    st, b, fl = _go(ia, "A", noted=True)
    assert st == "next" and b.added[-1][0] == "B" and fl == {"B"}
    assert "C1" in b.added[-1][1] and "noted" not in b.added[-1][1]
    st, b, fl = _go(ia, "B", noted=True)
    assert st == "stop" and ia._floor_speaker("#gen4") is None


def test_a_speak_tail_notes_stops():
    ia = _round(["A", "B", "C"])
    _go(ia, "A", noted=False, text="hi")
    _go(ia, "B", noted=True)
    st, b, _ = _go(ia, "C", noted=True)
    assert st == "stop"
    assert b.added == []


def test_a_speak_b_speak_c_noted_wraps_a():
    ia = _round(["A", "B", "C"])
    _go(ia, "A", noted=False, text="a")
    _go(ia, "B", noted=False, text="b")
    st, b, fl = _go(ia, "C", noted=True)
    assert st == "wrap" and b.added[-1][0] == "A" and "A" in fl


def test_a_noted_b_speak_c_noted_wraps_a():
    ia = _round(["A", "B", "C"])
    _go(ia, "A", noted=True)
    _go(ia, "B", noted=False, text="b")
    st, b, _ = _go(ia, "C", noted=True)
    assert st == "wrap" and b.added[-1][0] == "A"


def test_wrap_then_a_notes_stops():
    # A noted → B speak → C noted → A noted → stop
    ia = _round(["A", "B", "C"])
    _go(ia, "A", noted=True)
    _go(ia, "B", noted=False, text="b")
    _go(ia, "C", noted=True)
    st, b, _ = _go(ia, "A", noted=True)
    assert st == "stop"
    assert ia._floor_speaker("#gen4") is None


def test_non_seat_held():
    ia = _round(["Holt", "Squiggy", "Wend"])
    st, b, _ = _go(ia, "Wend", noted=False, text="nope")
    assert st == "held"
    assert b.added == []
