"""irc_rooms.json roster helpers."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters"))

import irc_rooms  # noqa: E402


def test_add_room_and_member(tmp_path: Path):
    p = tmp_path / "irc_rooms.json"
    ch = irc_rooms.add_room("standup", path=p)
    assert ch == "#standup"
    assert irc_rooms.rooms(p) == {"#standup": []}
    irc_rooms.add_member("#standup", "Trip-G", path=p)
    irc_rooms.add_member("standup", "Squiggy", path=p)
    assert irc_rooms.members_of("#standup", p) == ["Trip-G", "Squiggy"]
    assert irc_rooms.all_agents(p) == ["Trip-G", "Squiggy"]
    irc_rooms.remove_member("#standup", "Trip-G", path=p)
    assert irc_rooms.members_of("#standup", p) == ["Squiggy"]
    data = json.loads(p.read_text())
    assert data["rooms"]["#standup"] == ["Squiggy"]
    irc_rooms.add_room("#empty", path=p)
    assert irc_rooms.open_tabs(p) == ["#standup"]  # empty room not auto-opened
    irc_rooms.set_open_tabs(["#standup", "#empty"], path=p)
    assert irc_rooms.open_tabs(p) == ["#standup", "#empty"]
    irc_rooms.set_open_tabs(["#standup"], path=p)
    assert irc_rooms.open_tabs(p) == ["#standup"]


def test_room_awareness_cmd_join_and_leave():
    assert irc_rooms.room_awareness_cmd("standup", join=True) == {
        "action": "awareness",
        "add": "#standup",
        "mode": "doorbell",
    }
    assert irc_rooms.room_awareness_cmd("#standup", join=False) == {
        "action": "awareness",
        "remove": "#standup",
    }
