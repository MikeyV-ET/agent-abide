"""Agent tab bar overflow layout — no mid-tab black clips."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))

from nav_widgets import layout_agent_tabs, AgentTabBar


AGENTS = ["Sr", "Jr", "Trip", "Trip-G", "Q", "Cinco", "Squiggy", "MockTestAgent"]
TABS = AGENTS + [AgentTabBar.ROOM_TAB]


def test_all_fit_when_wide_enough():
    layout = layout_agent_tabs(TABS, "Trip", width=200, scroll=0)
    labels = [s[1] for s in layout["segments"] if s[3] == "tab"]
    assert "MockTestAgent" in labels
    assert "Room" in labels
    assert layout["hidden_right"] == 0
    assert layout["hidden_left"] == 0


def test_narrow_no_mid_tab_and_overflow_hint():
    layout = layout_agent_tabs(TABS, "Sr", width=40, scroll=0)
    total = sum(s[2] for s in layout["segments"])
    assert total <= 40, f"segments exceed width: {total} > 40 {layout['segments']}"
    # Only whole tabs
    for tab, lab, w, kind in layout["segments"]:
        if kind == "tab":
            assert w == len(lab) + 4
    assert layout["hidden_right"] > 0
    kinds = [s[3] for s in layout["segments"]]
    assert "right_hint" in kinds


def test_active_always_visible_when_scrolled_away():
    # Active is last agent; scroll at 0 with narrow width should re-home
    layout = layout_agent_tabs(TABS, "MockTestAgent", width=36, scroll=0)
    visible = [s[0] for s in layout["segments"] if s[3] == "tab"]
    assert "MockTestAgent" in visible, f"active missing: {layout}"


def test_active_visible_from_high_scroll():
    layout = layout_agent_tabs(TABS, "Sr", width=36, scroll=5)
    visible = [s[0] for s in layout["segments"] if s[3] == "tab"]
    assert "Sr" in visible


def test_long_name_ellipsized_alone():
    tabs = ["SuperCalifragilisticAgent", "#room"]
    layout = layout_agent_tabs(tabs, "SuperCalifragilisticAgent", width=20, scroll=0)
    tab_segs = [s for s in layout["segments"] if s[3] == "tab"]
    assert len(tab_segs) >= 1
    lab = tab_segs[0][1]
    assert lab.endswith("…") or len(lab) <= 16
    assert sum(s[2] for s in layout["segments"]) <= 20


def test_click_positions_cover_only_full_tabs():
    layout = layout_agent_tabs(TABS, "Jr", width=50, scroll=0)
    pos = 0
    for tab, lab, w, kind in layout["segments"]:
        assert w > 0
        pos += w
    assert pos <= 50
