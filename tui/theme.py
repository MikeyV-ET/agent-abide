"""Color themes for asdaaas TUI. Mutable active Theme via set_theme()."""
from __future__ import annotations

import json
from pathlib import Path


class GruvboxDark:
    """Gruvbox dark mode color palette."""
    NAME = "Gruvbox Dark"
    BG = "#282828"
    FG = "#ebdbb2"
    GRAY = "#928374"
    RED = "#cc241d"
    GREEN = "#98971a"
    YELLOW = "#d79921"
    BLUE = "#458588"
    PURPLE = "#b16286"
    AQUA = "#689d6a"
    ORANGE = "#d65d0e"
    BR_RED = "#fb4934"
    BR_GREEN = "#b8bb26"
    BR_YELLOW = "#fabd2f"
    BR_BLUE = "#83a598"
    BR_PURPLE = "#d3869b"
    BR_AQUA = "#8ec07c"
    BR_ORANGE = "#fe8019"
    DARK1 = "#3c3836"
    DARK2 = "#504945"
    DARK3 = "#665c54"
    DARK4 = "#7c6f64"


class GruvboxLight:
    """Gruvbox light mode color palette."""
    NAME = "Gruvbox Light"
    BG = "#fbf1c7"
    FG = "#3c3836"
    GRAY = "#928374"
    RED = "#cc241d"
    GREEN = "#98971a"
    YELLOW = "#d79921"
    BLUE = "#458588"
    PURPLE = "#b16286"
    AQUA = "#689d6a"
    ORANGE = "#d65d0e"
    BR_RED = "#9d0006"
    BR_GREEN = "#79740e"
    BR_YELLOW = "#b57614"
    BR_BLUE = "#076678"
    BR_PURPLE = "#8f3f71"
    BR_AQUA = "#427b58"
    BR_ORANGE = "#af3a03"
    DARK1 = "#ebdbb2"
    DARK2 = "#d5c4a1"
    DARK3 = "#bdae93"
    DARK4 = "#a89984"


class SolarizedDark:
    """Solarized dark color palette."""
    NAME = "Solarized Dark"
    BG = "#002b36"
    FG = "#839496"
    GRAY = "#586e75"
    RED = "#dc322f"
    GREEN = "#859900"
    YELLOW = "#b58900"
    BLUE = "#268bd2"
    PURPLE = "#6c71c4"
    AQUA = "#2aa198"
    ORANGE = "#cb4b16"
    BR_RED = "#dc322f"
    BR_GREEN = "#859900"
    BR_YELLOW = "#b58900"
    BR_BLUE = "#268bd2"
    BR_PURPLE = "#6c71c4"
    BR_AQUA = "#2aa198"
    BR_ORANGE = "#cb4b16"
    DARK1 = "#073642"
    DARK2 = "#586e75"
    DARK3 = "#657b83"
    DARK4 = "#839496"


THEMES = {
    "gruvbox-dark": GruvboxDark,
    "gruvbox-light": GruvboxLight,
    "solarized-dark": SolarizedDark,
}

THEME_CONFIG_FILE = Path.home() / ".config" / "abidetui" / "theme.json"


def _load_saved_theme() -> str:
    try:
        with open(THEME_CONFIG_FILE) as f:
            return json.load(f).get("theme", "gruvbox-dark")
    except (FileNotFoundError, json.JSONDecodeError):
        return "gruvbox-dark"


def _save_theme(name: str) -> None:
    THEME_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(THEME_CONFIG_FILE, "w") as f:
        json.dump({"theme": name}, f)


class _ThemeProxy:
    """Attribute access delegates to active palette; set_theme swaps it globally."""

    def __init__(self):
        self._palette = THEMES.get(_load_saved_theme(), GruvboxDark)

    def set(self, palette) -> None:
        self._palette = palette

    def current(self):
        return self._palette

    def __getattr__(self, name: str):
        return getattr(self._palette, name)


Theme = _ThemeProxy()


def set_theme(key: str) -> bool:
    if key not in THEMES:
        return False
    Theme.set(THEMES[key])
    _save_theme(key)
    return True
