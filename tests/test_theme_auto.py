import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))
import theme

def test_grok_presets_present():
    theme.reload_themes()
    assert "grokday" in theme.THEMES
    assert "groknight" in theme.THEMES
    assert theme.THEMES["grokday"].BG.startswith("#f")  # light-ish

def test_resolve_aliases(monkeypatch):
    theme.reload_themes()
    monkeypatch.setattr(theme, "detect_system_appearance", lambda: "light")
    assert theme.resolve_theme_key("auto") in ("grokday", "gruvbox-light")
    monkeypatch.setattr(theme, "detect_system_appearance", lambda: "dark")
    assert theme.resolve_theme_key("auto") in ("groknight", "grouvbox-dark")
    assert theme.resolve_theme_key("light") == "grokday"
    assert theme.resolve_theme_key("day") == "grokday"

def test_apply_auto_if_needed(monkeypatch):
    theme.reload_themes()
    theme.set_theme("auto")
    # force appearance flip
    monkeypatch.setattr(theme, "detect_system_appearance", lambda: "light")
    changed = theme.apply_auto_if_needed()
    # may or may not change depending on prior resolve; call twice
    theme.Theme._key = "groknight"
    theme.Theme.set(theme.THEMES["groknight"])
    assert theme.apply_auto_if_needed() is True
    assert theme.Theme.current_key() == "grokday"
