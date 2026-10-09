"""Collapsed tool rows must paint empty cells, not skip to the border."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))
from rich.text import Text
from chat_widgets import fill_line_to_width


def test_fill_extends_plain_to_inner_width():
    t = Text("ab")
    fill_line_to_width(t, 5, "#3c3836")
    assert t.cell_len == 5
    assert t.plain == "ab   "


def test_fill_noop_when_already_wide():
    t = Text("abcdef")
    fill_line_to_width(t, 4, "#3c3836")
    assert t.cell_len == 6
    assert t.plain == "abcdef"


def test_fill_noop_on_zero_inner():
    t = Text("x")
    fill_line_to_width(t, 0, "#3c3836")
    assert t.plain == "x"
