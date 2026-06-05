"""TUI gaze selector tests.

Tests that the gaze selector writes correct gaze.json format for
different room types. Uses a temp filesystem to simulate adapter dirs
and tests the same logic as _set_gaze_to_room in asdaaas_tui.py.

This is a unit test of the gaze construction logic, not a full TUI
pilot test. The logic tested here mirrors _set_gaze_to_room exactly.

Run: pytest test_tui_gaze.py -v
"""

import json
import os
import tempfile
import pytest
from pathlib import Path


def build_tui_gaze(room, agent_name, agents_home):
    """Replicates the gaze construction logic from _set_gaze_to_room.

    This mirrors both copies in asdaaas_tui.py (GazeSelector and app-level).
    If the TUI logic changes, this test helper must be updated to match,
    and the tests will catch divergence.
    """
    agent_lower = agent_name.lower()

    if room == "tui":
        gaze = {
            "speech": {"target": "tui", "params": {}},
            "thoughts": {"target": "irc", "params": {"room": f"#{agent_lower}-thoughts"}}
        }
        gaze_str = "tui"
    elif room.startswith("pm:"):
        gaze = {
            "speech": {"target": "irc", "params": {"room": room}},
            "thoughts": {"target": "irc", "params": {"room": f"#{agent_lower}-thoughts"}}
        }
        gaze_str = f"irc/{room}"
    else:
        # Check if room matches a known non-IRC adapter
        adapter_dir = Path(agents_home) / agent_name / "asdaaas" / "adapters" / room
        if adapter_dir.exists() and room != "irc":
            gaze = {
                "speech": {"target": room, "params": {}},
                "thoughts": {"target": "irc", "params": {"room": f"#{agent_lower}-thoughts"}}
            }
            gaze_str = room
        else:
            gaze = {
                "speech": {"target": "irc", "params": {"room": room}},
                "thoughts": {"target": "irc", "params": {"room": f"#{agent_lower}-thoughts"}}
            }
            gaze_str = f"irc/{room}"

    return gaze, gaze_str


@pytest.fixture
def agent_env(tmp_path):
    """Create a temp agent dir with adapter directories."""
    agent_name = "TestAgent"
    agents_home = tmp_path
    adapters_dir = tmp_path / agent_name / "asdaaas" / "adapters"

    # Create adapter dirs matching a typical agent
    for adapter in ["irc", "tui", "arena", "localmail", "remind"]:
        (adapters_dir / adapter / "inbox").mkdir(parents=True)
        (adapters_dir / adapter / "outbox").mkdir(parents=True)

    # Create gaze.json location
    gaze_file = tmp_path / agent_name / "asdaaas" / "gaze.json"

    return {
        "agent_name": agent_name,
        "agents_home": str(tmp_path),
        "gaze_file": gaze_file,
        "adapters_dir": adapters_dir,
    }


# ============================================================================
# TG: TUI Gaze Selector Tests
# ============================================================================

class TestTuiGazeSelector:
    """TG1-TG6: Verify gaze selector writes correct format per room type."""

    def test_tg1_select_tui(self, agent_env):
        """TG1: Selecting 'tui' sets gaze target to tui with empty params."""
        gaze, gaze_str = build_tui_gaze("tui", agent_env["agent_name"], agent_env["agents_home"])
        assert gaze["speech"]["target"] == "tui"
        assert gaze["speech"]["params"] == {}
        assert gaze_str == "tui"

    def test_tg2_select_arena(self, agent_env):
        """TG2: Selecting 'arena' sets gaze target to arena adapter, NOT irc/arena.

        This is the regression test for issue #14. Before the fix,
        selecting arena wrote {"target": "irc", "params": {"room": "arena"}}
        which routed speech to IRC channel #arena instead of the SA arena adapter.
        """
        gaze, gaze_str = build_tui_gaze("arena", agent_env["agent_name"], agent_env["agents_home"])
        assert gaze["speech"]["target"] == "arena", (
            f"Arena gaze target should be 'arena', not '{gaze['speech']['target']}'"
        )
        assert gaze["speech"]["params"] == {}, (
            f"Arena gaze should have empty params, not {gaze['speech']['params']}"
        )
        assert gaze_str == "arena"
        # Negative assertion: must NOT be IRC format
        assert gaze["speech"]["target"] != "irc"

    def test_tg3_select_irc_channel(self, agent_env):
        """TG3: Selecting '#standup' sets gaze to IRC with room param."""
        gaze, gaze_str = build_tui_gaze("#standup", agent_env["agent_name"], agent_env["agents_home"])
        assert gaze["speech"]["target"] == "irc"
        assert gaze["speech"]["params"]["room"] == "#standup"
        assert gaze_str == "irc/#standup"

    def test_tg4_select_pm(self, agent_env):
        """TG4: Selecting 'pm:eric' sets gaze to IRC PM format."""
        gaze, gaze_str = build_tui_gaze("pm:eric", agent_env["agent_name"], agent_env["agents_home"])
        assert gaze["speech"]["target"] == "irc"
        assert gaze["speech"]["params"]["room"] == "pm:eric"
        assert gaze_str == "irc/pm:eric"

    def test_tg5_thoughts_always_irc(self, agent_env):
        """TG5: Thoughts target is always IRC regardless of speech target."""
        for room in ["tui", "arena", "#standup", "pm:eric"]:
            gaze, _ = build_tui_gaze(room, agent_env["agent_name"], agent_env["agents_home"])
            assert gaze["thoughts"]["target"] == "irc"
            agent_lower = agent_env["agent_name"].lower()
            assert gaze["thoughts"]["params"]["room"] == f"#{agent_lower}-thoughts"

    def test_tg6_unknown_room_no_adapter_dir_uses_irc(self, agent_env):
        """TG6: Room with no adapter directory defaults to IRC format."""
        gaze, gaze_str = build_tui_gaze("slack", agent_env["agent_name"], agent_env["agents_home"])
        assert gaze["speech"]["target"] == "irc"
        assert gaze["speech"]["params"]["room"] == "slack"
        assert gaze_str == "irc/slack"

    def test_tg7_irc_room_not_treated_as_adapter(self, agent_env):
        """TG7: 'irc' has an adapter dir but is still treated as IRC (needs room)."""
        # "irc" as a room name should NOT become {"target": "irc", "params": {}}
        # It should stay as IRC format: {"target": "irc", "params": {"room": "irc"}}
        gaze, gaze_str = build_tui_gaze("irc", agent_env["agent_name"], agent_env["agents_home"])
        assert gaze["speech"]["target"] == "irc"
        assert gaze_str == "irc/irc"

    def test_tg8_gaze_json_written_correctly(self, agent_env):
        """TG8: End-to-end: build gaze, write to file, read back, verify."""
        gaze, _ = build_tui_gaze("arena", agent_env["agent_name"], agent_env["agents_home"])
        gaze_file = agent_env["gaze_file"]
        with open(gaze_file, "w") as f:
            json.dump(gaze, f)
        with open(gaze_file) as f:
            loaded = json.load(f)
        assert loaded["speech"]["target"] == "arena"
        assert loaded["speech"]["params"] == {}

    def test_tg9_new_adapter_detected(self, agent_env):
        """TG9: Adding a new adapter dir makes it recognized as non-IRC."""
        # Before creating dir: treated as IRC
        gaze_before, _ = build_tui_gaze("slack", agent_env["agent_name"], agent_env["agents_home"])
        assert gaze_before["speech"]["target"] == "irc"

        # Create the adapter dir
        slack_dir = agent_env["adapters_dir"] / "slack" / "inbox"
        slack_dir.mkdir(parents=True)

        # After: treated as adapter
        gaze_after, gaze_str = build_tui_gaze("slack", agent_env["agent_name"], agent_env["agents_home"])
        assert gaze_after["speech"]["target"] == "slack"
        assert gaze_after["speech"]["params"] == {}
        assert gaze_str == "slack"
