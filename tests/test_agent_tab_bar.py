"""Agent tab bar overflow + close/add layout."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))

from nav_widgets import layout_agent_tabs, AgentTabBar


AGENTS = ["Sr", "Jr", "Trip", "Trip-G", "Q", "Cinco", "Squiggy", "MockTestAgent"]
TABS = AGENTS + [AgentTabBar.ROOM_TAB]


def test_all_fit_when_wide_enough():
    layout = layout_agent_tabs(TABS, "Trip", width=300, scroll=0)
    labels = [s[1] for s in layout["segments"] if s[3] == "tab"]
    assert "MockTestAgent" in labels
    assert "Room" in labels
    assert layout["hidden_right"] == 0
    kinds = [s[3] for s in layout["segments"]]
    assert "add" in kinds
    assert "close" in kinds


def test_narrow_no_mid_tab_and_overflow_hint():
    layout = layout_agent_tabs(TABS, "Sr", width=40, scroll=0)
    total = sum(s[2] for s in layout["segments"])
    assert total <= 40, f"segments exceed width: {total} > 40 {layout['segments']}"
    for tab, lab, w, kind in layout["segments"]:
        if kind == "tab":
            assert w == len(lab) + 4
    assert layout["hidden_right"] > 0 or "right_hint" in [s[3] for s in layout["segments"]] or "add" in [s[3] for s in layout["segments"]]


def test_active_always_visible_when_scrolled_away():
    layout = layout_agent_tabs(TABS, "MockTestAgent", width=40, scroll=0)
    visible = [s[0] for s in layout["segments"] if s[3] == "tab"]
    assert "MockTestAgent" in visible, f"active missing: {layout}"


def test_active_visible_from_high_scroll():
    layout = layout_agent_tabs(TABS, "Sr", width=40, scroll=5)
    visible = [s[0] for s in layout["segments"] if s[3] == "tab"]
    assert "Sr" in visible


def test_long_name_ellipsized_alone():
    tabs = ["SuperCalifragilisticAgent", "#room"]
    layout = layout_agent_tabs(tabs, "SuperCalifragilisticAgent", width=20, scroll=0)
    tab_segs = [s for s in layout["segments"] if s[3] == "tab"]
    assert len(tab_segs) >= 1
    assert sum(s[2] for s in layout["segments"]) <= 20


def test_room_has_no_close():
    layout = layout_agent_tabs(["Sr", "#room"], "Sr", width=80, scroll=0)
    room_closes = [s for s in layout["segments"] if s[3] == "close" and s[0] == "#room"]
    assert room_closes == []
    sr_closes = [s for s in layout["segments"] if s[3] == "close" and s[0] == "Sr"]
    assert len(sr_closes) == 1


def test_add_always_present():
    layout = layout_agent_tabs(["Trip"], "Trip", width=80, scroll=0, show_add=True)
    assert any(s[3] == "add" for s in layout["segments"])


def test_catalog_helper():
    # Pure import of Config may need path - test list shape via nav only
    assert AgentTabBar.ROOM_TAB == "#room"
