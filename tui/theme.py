"""Color themes for asdaaas TUI.

Themes load from:
  1. Built-in defaults (always present)
  2. JSON files in tui/themes/*.json (override / add)

Add a theme: copy an existing JSON in tui/themes/, edit colors, restart TUI
(or call reload_themes() then re-open Ctrl+T picker).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any


THEME_CONFIG_FILE = Path.home() / ".config" / "abidetui" / "theme.json"
THEMES_DIR = Path(__file__).resolve().parent / "themes"

# Required color keys (match historical palette attributes)
COLOR_KEYS = [
    "BG", "FG", "GRAY", "RED", "GREEN", "YELLOW", "BLUE", "PURPLE", "AQUA", "ORANGE",
    "BR_RED", "BR_GREEN", "BR_YELLOW", "BR_BLUE", "BR_PURPLE", "BR_AQUA", "BR_ORANGE",
    "DARK1", "DARK2", "DARK3", "DARK4",
]

# Built-in fallbacks if JSON missing
_BUILTIN: dict[str, dict[str, Any]] = {
    "gruvbox-dark": {
        "name": "Gruvbox Dark",
        "colors": {
            "BG": "#282828", "FG": "#ebdbb2", "GRAY": "#928374",
            "RED": "#cc241d", "GREEN": "#98971a", "YELLOW": "#d79921",
            "BLUE": "#458588", "PURPLE": "#b16286", "AQUA": "#689d6a", "ORANGE": "#d65d0e",
            "BR_RED": "#fb4934", "BR_GREEN": "#b8bb26", "BR_YELLOW": "#fabd2f",
            "BR_BLUE": "#83a598", "BR_PURPLE": "#d3869b", "BR_AQUA": "#8ec07c", "BR_ORANGE": "#fe8019",
            "DARK1": "#3c3836", "DARK2": "#504945", "DARK3": "#665c54", "DARK4": "#7c6f64",
        },
    },
    "gruvbox-light": {
        "name": "Gruvbox Light",
        "colors": {
            "BG": "#fbf1c7", "FG": "#3c3836", "GRAY": "#928374",
            "RED": "#cc241d", "GREEN": "#98971a", "YELLOW": "#d79921",
            "BLUE": "#458588", "PURPLE": "#b16286", "AQUA": "#689d6a", "ORANGE": "#d65d0e",
            "BR_RED": "#9d0006", "BR_GREEN": "#79740e", "BR_YELLOW": "#b57614",
            "BR_BLUE": "#076678", "BR_PURPLE": "#8f3f71", "BR_AQUA": "#427b58", "BR_ORANGE": "#af3a03",
            "DARK1": "#ebdbb2", "DARK2": "#d5c4a1", "DARK3": "#bdae93", "DARK4": "#a89984",
        },
    },
    "solarized-dark": {
        "name": "Solarized Dark",
        "colors": {
            "BG": "#002b36", "FG": "#839496", "GRAY": "#586e75",
            "RED": "#dc322f", "GREEN": "#859900", "YELLOW": "#b58900",
            "BLUE": "#268bd2", "PURPLE": "#6c71c4", "AQUA": "#2aa198", "ORANGE": "#cb4b16",
            "BR_RED": "#dc322f", "BR_GREEN": "#859900", "BR_YELLOW": "#b58900",
            "BR_BLUE": "#268bd2", "BR_PURPLE": "#6c71c4", "BR_AQUA": "#2aa198", "BR_ORANGE": "#cb4b16",
            "DARK1": "#073642", "DARK2": "#094959", "DARK3": "#586e75", "DARK4": "#657b83",
        },
    },
}


def _palette_from_colors(name: str, colors: dict[str, str]) -> SimpleNamespace:
    """Build attribute-style palette (Theme.FG etc.)."""
    base = dict(_BUILTIN["gruvbox-dark"]["colors"])
    base.update({k: v for k, v in colors.items() if k in COLOR_KEYS})
    ns = SimpleNamespace(**base)
    ns.NAME = name
    return ns


def _load_json_themes() -> dict[str, SimpleNamespace]:
    out: dict[str, SimpleNamespace] = {}
    if not THEMES_DIR.is_dir():
        return out
    for path in sorted(THEMES_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"[tui] skip theme {path.name}: {e}")
            continue
        tid = data.get("id") or path.stem
        name = data.get("name") or tid
        colors = data.get("colors") or {}
        out[tid] = _palette_from_colors(name, colors)
    return out


def reload_themes() -> dict[str, SimpleNamespace]:
    """Rebuild THEMES from builtins + tui/themes/*.json. Returns THEMES."""
    global THEMES
    themes: dict[str, SimpleNamespace] = {}
    for tid, meta in _BUILTIN.items():
        themes[tid] = _palette_from_colors(meta["name"], meta["colors"])
    themes.update(_load_json_themes())  # JSON overrides same id
    THEMES = themes
    return THEMES


# Module-level registry (mutated by reload_themes)
THEMES: dict[str, SimpleNamespace] = {}
reload_themes()


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
        key = _load_saved_theme()
        self._key = key if key in THEMES else "gruvbox-dark"
        self._palette = THEMES[self._key]

    def set(self, palette) -> None:
        self._palette = palette

    def current(self):
        return self._palette

    def current_key(self) -> str:
        return getattr(self, "_key", "gruvbox-dark")

    def __getattr__(self, name: str):
        return getattr(self._palette, name)


Theme = _ThemeProxy()


def set_theme(key: str) -> bool:
    if key not in THEMES:
        reload_themes()
    if key not in THEMES:
        return False
    Theme.set(THEMES[key])
    Theme._key = key
    _save_theme(key)
    return True


# Back-compat aliases for code that referenced class objects
GruvboxDark = THEMES["gruvbox-dark"]
GruvboxLight = THEMES["gruvbox-light"]
SolarizedDark = THEMES["solarized-dark"]
