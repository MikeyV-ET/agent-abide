"""Unit tests for gaze matching logic in asdaaas.py.

Tests the matches_gaze function and related routing behavior.
These document expected behavior for message delivery based on
agent gaze and adapter source.

Run: pytest test_gaze_matching.py -v
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))

from asdaaas import matches_gaze, get_msg_room, get_room, get_background_mode, _build_gaze


# ============================================================================
# Helper: build message and gaze dicts
# ============================================================================

def make_msg(adapter, room=None, sender="eric"):
    msg = {"adapter": adapter, "from": sender, "meta": {}}
    if room:
        msg["meta"]["room"] = room
    return msg


def make_gaze(adapter, room=None):
    gaze = {"speech": {"target": adapter, "params": {}}}
    if room:
        gaze["speech"]["params"]["room"] = room
    return gaze


def make_awareness(channels=None, default="pending"):
    return {
        "background_channels": channels or {},
        "background_default": default,
    }


# ============================================================================
# Basic gaze matching
# ============================================================================

class TestMatchesGaze:
    def test_same_adapter_no_room_matches(self):
        """Gaze on adapter with no room matches all messages on that adapter."""
        gaze = make_gaze("tui")
        msg = make_msg("tui", room="tui")
        assert matches_gaze(msg, gaze) is True

    def test_different_adapter_does_not_match(self):
        """Messages from different adapter don't match gaze."""
        gaze = make_gaze("arena")
        msg = make_msg("irc", room="#standup")
        assert matches_gaze(msg, gaze) is False

    def test_same_adapter_same_room_matches(self):
        """Exact adapter + room match."""
        gaze = make_gaze("irc", room="#standup")
        msg = make_msg("irc", room="#standup")
        assert matches_gaze(msg, gaze) is True

    def test_same_adapter_different_room_no_match(self):
        """Same adapter but different room doesn't match."""
        gaze = make_gaze("irc", room="#standup")
        msg = make_msg("irc", room="#meetingroom1")
        assert matches_gaze(msg, gaze) is False

    def test_no_gaze_matches_nothing(self):
        """No gaze set -> nothing is foreground."""
        gaze = {}
        msg = make_msg("tui")
        assert matches_gaze(msg, gaze) is False

    def test_arena_gaze_arena_msg_matches(self):
        """Agent gazing at arena receives arena messages as foreground."""
        gaze = make_gaze("arena")
        msg = make_msg("arena", room="arena")
        assert matches_gaze(msg, gaze) is True


# ============================================================================
# The operator-TUI-while-gazing-at-arena scenario (Jr's bug)
# ============================================================================

class TestOperatorTuiPriority:
    """Operator TUI messages are always foreground regardless of gaze.

    The TUI is the operator's direct interface. When an agent gazes at
    arena, IRC, or any other adapter, TUI messages still arrive as
    foreground — never backgrounded.
    """

    def test_tui_foreground_when_gazing_at_arena(self):
        """TUI messages are foreground even when gaze targets arena."""
        gaze = make_gaze("arena")
        msg = make_msg("tui", room="tui")
        assert matches_gaze(msg, gaze) is True

    def test_tui_foreground_when_gazing_at_irc(self):
        """TUI messages are foreground even when gaze targets IRC."""
        gaze = make_gaze("irc", room="#standup")
        msg = make_msg("tui", room="tui")
        assert matches_gaze(msg, gaze) is True

    def test_tui_still_background_when_no_gaze(self):
        """TUI messages are NOT foreground when gaze is empty (no active gaze)."""
        gaze = {}
        msg = make_msg("tui", room="tui")
        assert matches_gaze(msg, gaze) is False

    def test_tui_msg_goes_to_doorbell_when_in_awareness(self):
        """TUI messages at least get doorbell delivery via awareness."""
        awareness = make_awareness({"tui": "doorbell"})
        msg = make_msg("tui", room="tui")
        mode = get_background_mode(msg, awareness)
        assert mode == "doorbell"

    def test_non_tui_still_requires_adapter_match(self):
        """Non-TUI adapters still require adapter match (IRC doesn't match arena)."""
        gaze = make_gaze("arena")
        msg = make_msg("irc", room="#standup")
        assert matches_gaze(msg, gaze) is False


# ============================================================================
# Background mode routing
# ============================================================================

class TestBackgroundMode:
    def test_known_channel_uses_explicit_mode(self):
        awareness = make_awareness({"#standup": "doorbell", "tui": "doorbell"})
        msg = make_msg("irc", room="#standup")
        assert get_background_mode(msg, awareness) == "doorbell"

    def test_unknown_channel_uses_default(self):
        awareness = make_awareness({"#standup": "doorbell"}, default="pending")
        msg = make_msg("irc", room="#unknown")
        assert get_background_mode(msg, awareness) == "pending"

    def test_arena_in_awareness_gets_doorbell(self):
        awareness = make_awareness({"arena": "doorbell"})
        msg = make_msg("arena", room="arena")
        assert get_background_mode(msg, awareness) == "doorbell"


# ============================================================================
# _build_gaze: gaze construction from command queue commands
# ============================================================================

class TestBuildGaze:
    """Tests for _build_gaze() which constructs gaze dicts from commands.

    Covers the fix in 7458db3: non-IRC adapters (arena, tui) no longer
    require a room parameter and get empty params instead of returning None.
    """

    def test_irc_room(self):
        """IRC channel gaze: target=irc, params has room."""
        cmd = {"action": "gaze", "adapter": "irc", "room": "#standup"}
        gaze = _build_gaze(cmd)
        assert gaze["speech"] == {"target": "irc", "params": {"room": "#standup"}}

    def test_irc_pm(self):
        """IRC PM gaze: target=irc, params has pm:nick room."""
        cmd = {"action": "gaze", "adapter": "irc", "pm": "eric"}
        gaze = _build_gaze(cmd)
        assert gaze["speech"]["target"] == "irc"
        assert gaze["speech"]["params"]["pm"] == "eric"
        assert gaze["speech"]["params"]["room"] == "pm:eric"

    def test_arena_no_room(self):
        """Arena adapter gaze: target=arena, empty params (no room needed)."""
        cmd = {"action": "gaze", "adapter": "arena"}
        gaze = _build_gaze(cmd)
        assert gaze is not None, "_build_gaze returned None for arena adapter"
        assert gaze["speech"] == {"target": "arena", "params": {}}

    def test_tui_no_room(self):
        """TUI adapter gaze: target=tui, empty params."""
        cmd = {"action": "gaze", "adapter": "tui"}
        gaze = _build_gaze(cmd)
        assert gaze is not None, "_build_gaze returned None for tui adapter"
        assert gaze["speech"] == {"target": "tui", "params": {}}

    def test_gaze_off(self):
        """Gaze off clears both speech and thoughts."""
        cmd = {"action": "gaze", "off": True}
        gaze = _build_gaze(cmd)
        assert gaze["speech"] is None
        assert gaze["thoughts"] is None

    def test_thoughts_channel(self):
        """Thoughts target set when thoughts key provided."""
        cmd = {"action": "gaze", "adapter": "irc", "room": "#standup", "thoughts": "#trip-thoughts"}
        gaze = _build_gaze(cmd)
        assert gaze["thoughts"]["target"] == "irc"
        assert gaze["thoughts"]["params"]["room"] == "#trip-thoughts"

    def test_no_thoughts_is_none(self):
        """Thoughts is None when not specified."""
        cmd = {"action": "gaze", "adapter": "arena"}
        gaze = _build_gaze(cmd)
        assert gaze["thoughts"] is None

    def test_no_adapter_returns_none(self):
        """Missing adapter key returns None (invalid command)."""
        cmd = {"action": "gaze"}
        assert _build_gaze(cmd) is None

    def test_unknown_adapter_no_room(self):
        """Any unknown adapter without room gets empty params (not None)."""
        cmd = {"action": "gaze", "adapter": "slack"}
        gaze = _build_gaze(cmd)
        assert gaze is not None
        assert gaze["speech"] == {"target": "slack", "params": {}}
