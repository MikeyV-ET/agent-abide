import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))
from pathlib import Path
import theme

def test_builtin_themes_loaded():
    theme.reload_themes()
    assert "gruvbox-dark" in theme.THEMES
    assert theme.THEMES["gruvbox-dark"].FG.startswith("#")

def test_set_theme():
    assert theme.set_theme("gruvbox-light")
    assert theme.Theme.NAME == "Gruvbox Light"
    assert theme.set_theme("gruvbox-dark")

def test_json_theme_roundtrip(tmp_path, monkeypatch):
    # point themes dir at temp with a custom theme
    custom = {
        "id": "test-pink",
        "name": "Test Pink",
        "colors": {**theme._BUILTIN["gruvbox-dark"]["colors"], "FG": "#ff00aa"},
    }
    d = tmp_path / "themes"
    d.mkdir()
    (d / "test-pink.json").write_text(json.dumps(custom))
    monkeypatch.setattr(theme, "THEMES_DIR", d)
    theme.reload_themes()
    assert "test-pink" in theme.THEMES
    assert theme.THEMES["test-pink"].FG == "#ff00aa"
    assert theme.set_theme("test-pink")
    assert theme.Theme.FG == "#ff00aa"
    theme.set_theme("gruvbox-dark")
