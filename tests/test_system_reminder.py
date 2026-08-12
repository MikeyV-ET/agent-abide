import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))
from chat_widgets import is_system_reminder, SystemReminderPanel
from chat_model import ChatState, apply_event, SystemItem

def test_is_system_reminder():
    assert is_system_reminder("<system-reminder> Background task \"x\" completed")
    assert is_system_reminder("  <system-reminder> hi")
    assert not is_system_reminder("<eric (via tui)> hello")
    assert not is_system_reminder("normal text")

def test_panel_title_and_collapsed():
    t = '<system-reminder> Background task "call-0dd994c8-f6ef" completed (exit code: 0).\nCommand: foo\nbar\nbaz\n'
    p = SystemReminderPanel(t)
    assert p._collapsed
    assert "task" in p._title
    r = p.render()
    assert r is not None

def test_chat_model_no_turn_bump():
    s = ChatState()
    ev = {"params": {"update": {
        "sessionUpdate": "user_message_chunk",
        "content": {"text": "<system-reminder> Background task done"},
    }}}
    apply_event(s, ev)
    assert s.logical_turn == 0
    assert isinstance(s.items[0], SystemItem)
    assert s.items[0].kind == "system_reminder"
