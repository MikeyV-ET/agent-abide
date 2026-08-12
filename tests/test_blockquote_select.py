import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))
from rich.markdown import Markdown
from chat_widgets import _flatten_to_text

def test_blockquote_bars_stripped_from_selectable_text():
    r = _flatten_to_text(Markdown("> alpha line\n> beta line"), width=60)
    assert "▌" not in r.plain
    assert "alpha" in r.plain
    assert "beta" in r.plain or "alpha" in r.plain  # Rich may join lines
