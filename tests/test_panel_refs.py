import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))
from chat_widgets import short_ref, ToolCallPanel, InterjectionBlock

def test_short_ref_call_id():
    assert short_ref("call-a6f1a470-70ef-4508") == "id:a6f1a470"

def test_short_ref_bell():
    assert short_ref("bell_32ac0874") == "bell:32ac0874"

def test_tool_panel_title_includes_ref():
    p = ToolCallPanel("call-deadbeef-1234", "tool")
    p.render()
    assert "id:deadbeef" in p.border_title
    body = p.render()
    assert "cite id:deadbeef" in body.plain

def test_interjection_ref():
    b = InterjectionBlock("[eric (via tui) (id=bell_32ac0874, ts=Tue)] hello")
    b.render()
    assert "bell:32ac0874" in b.border_title
