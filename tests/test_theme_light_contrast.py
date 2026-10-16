"""Gruvbox light: no ANSI cyan on tan panes (Eric 2026-10-15)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tui"))

from rich.markdown import Markdown
from theme import Theme, set_theme, theme_css
from chat_widgets import _flatten_to_text


def test_light_markdown_link_is_palette_blue_not_ansi_cyan():
    set_theme("gruvbox-light")
    t = _flatten_to_text(Markdown("[docs](https://example.com)"), width=40)
    assert "docs" in t.plain
    colors = []
    for span in t.spans:
        if span.style and span.style.color:
            colors.append(span.style.color.name if hasattr(span.style.color, "name") else str(span.style.color))
    blob = " ".join(colors).lower()
    assert "cyan" not in blob
    assert "#00ffff" not in blob
    assert Theme.BLUE.lstrip("#").lower() in blob.replace("#", "") or Theme.BR_BLUE.lower() in blob


def test_theme_css_sets_accent_to_br_blue():
    set_theme("gruvbox-light")
    css = theme_css()
    assert Theme.BR_BLUE in css
    assert "$accent:" in css


def test_light_inline_code_is_dark_on_tan_chip():
    from theme import set_theme, Theme
    from rich.markdown import Markdown
    from chat_widgets import _flatten_to_text
    set_theme("gruvbox-light")
    t = _flatten_to_text(Markdown("`013f5f0`"), width=20)
    blob = " ".join(str(s.style) for s in t.spans if s.style).lower()
    assert Theme.FG.lower() in blob
    assert Theme.DARK2.lower() in blob or "d5c4a1" in blob


def test_near_black_hex_stays_on_code_chip_not_black_box():
    from theme import set_theme, Theme
    from rich.markdown import Markdown
    from chat_widgets import _flatten_to_text
    set_theme("gruvbox-light")
    t = _flatten_to_text(Markdown("`#3c3836`"), width=20)
    blob = " ".join(str(s.style) for s in t.spans).lower()
    # generic code chip, not a #3c3836 fill
    assert Theme.DARK2.lower() in blob or "d5c4a1" in blob
    assert "on #3c3836" not in blob


def test_mid_hex_is_swatch():
    from theme import set_theme
    from rich.markdown import Markdown
    from chat_widgets import _flatten_to_text
    set_theme("gruvbox-light")
    t = _flatten_to_text(Markdown("`#076678`"), width=20)
    blob = " ".join(str(s.style) for s in t.spans).lower()
    assert "076678" in blob
