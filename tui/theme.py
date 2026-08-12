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


def load_theme_config() -> dict[str, Any]:
    """Load ~/.config/abidetui/theme.json (theme + auto mappings)."""
    defaults = {
        "theme": "gruvbox-dark",
        "auto_dark_theme": "groknight",
        "auto_light_theme": "grokday",
    }
    try:
        with open(THEME_CONFIG_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return defaults
        defaults.update({k: v for k, v in data.items() if v is not None})
        return defaults
    except (FileNotFoundError, json.JSONDecodeError):
        return defaults


def save_theme_config(cfg: dict[str, Any]) -> None:
    THEME_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(THEME_CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def _load_saved_theme() -> str:
    return str(load_theme_config().get("theme") or "gruvbox-dark")


def _save_theme(name: str) -> None:
    cfg = load_theme_config()
    cfg["theme"] = name
    save_theme_config(cfg)


def detect_system_appearance() -> str:
    """Return 'dark' or 'light' (default dark if unknown).

    Linux (in order):
      1. gsettings org.gnome.desktop.interface color-scheme
      2. XDG Desktop Portal Settings Read color-scheme (0=no pref, 1=dark, 2=light)
    """
    import shutil
    import subprocess

    if shutil.which("gsettings"):
        try:
            r = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                capture_output=True, text=True, timeout=1.5,
            )
            if r.returncode == 0:
                val = (r.stdout or "").strip().strip("'\"")
                if "prefer-light" in val:
                    return "light"
                if "prefer-dark" in val:
                    return "dark"
        except Exception:
            pass

    if shutil.which("gdbus"):
        try:
            r = subprocess.run(
                [
                    "gdbus", "call", "--session",
                    "--dest", "org.freedesktop.portal.Desktop",
                    "--object-path", "/org/freedesktop/portal/desktop",
                    "--method", "org.freedesktop.portal.Settings.Read",
                    "org.freedesktop.appearance", "color-scheme",
                ],
                capture_output=True, text=True, timeout=1.5,
            )
            out = r.stdout or ""
            if "uint32 2" in out or ", 2)" in out:
                return "light"
            if "uint32 1" in out or ", 1)" in out:
                return "dark"
        except Exception:
            pass

    return "dark"


def resolve_theme_key(key: str | None = None) -> str:
    """Map 'auto' / aliases to a concrete theme id."""
    reload_themes()
    cfg = load_theme_config()
    raw = (key if key is not None else cfg.get("theme")) or "gruvbox-dark"
    raw = str(raw).lower().strip()
    if raw in ("auto", "system"):
        appearance = detect_system_appearance()
        if appearance == "light":
            cand = cfg.get("auto_light_theme") or "grokday"
        else:
            cand = cfg.get("auto_dark_theme") or "groknight"
        cand = str(cand)
        if cand not in THEMES:
            cand = "gruvbox-light" if appearance == "light" else "gruvbox-dark"
        return cand
    # aliases
    aliases = {
        "dark": "groknight",
        "light": "grokday",
        "day": "grokday",
        "night": "groknight",
    }
    raw = aliases.get(raw, raw)
    if raw not in THEMES:
        return "gruvbox-dark"
    return raw


class _ThemeProxy:
    """Attribute access delegates to active palette; set_theme swaps it globally."""

    def __init__(self):
        self._preference = _load_saved_theme()  # may be "auto"
        resolved = resolve_theme_key(self._preference)
        self._key = resolved
        self._palette = THEMES.get(resolved) or THEMES["gruvbox-dark"]

    def set(self, palette) -> None:
        self._palette = palette

    def current(self):
        return self._palette

    def current_key(self) -> str:
        return getattr(self, "_key", "gruvbox-dark")

    def preference(self) -> str:
        """User preference including 'auto'."""
        return getattr(self, "_preference", self.current_key())

    def __getattr__(self, name: str):
        return getattr(self._palette, name)


Theme = _ThemeProxy()


def set_theme(key: str) -> bool:
    """Apply theme by id, or 'auto'/'system' for OS appearance.

    Saves preference to config. Returns False if concrete theme cannot be resolved.
    """
    reload_themes()
    pref = key.lower().strip()
    resolved = resolve_theme_key(pref)
    if resolved not in THEMES:
        return False
    Theme.set(THEMES[resolved])
    Theme._key = resolved
    Theme._preference = pref if pref in ("auto", "system") or pref in THEMES or pref in (
        "dark", "light", "day", "night"
    ) else resolved
    # Normalize stored preference
    if pref in ("system",):
        pref = "auto"
    if pref in ("dark", "night"):
        # store as concrete unless user wanted alias — store concrete for clarity
        pass
    _save_theme(pref if pref in THEMES or pref == "auto" else resolved)
    Theme._preference = load_theme_config().get("theme", resolved)
    return True


def apply_auto_if_needed() -> bool:
    """If preference is auto, re-resolve from OS and apply if concrete theme changed.

    Returns True if the active palette changed.
    """
    pref = Theme.preference()
    if str(pref).lower() not in ("auto", "system"):
        return False
    resolved = resolve_theme_key("auto")
    if resolved == Theme.current_key() and THEMES.get(resolved) is Theme.current():
        return False
    if resolved not in THEMES:
        return False
    Theme.set(THEMES[resolved])
    Theme._key = resolved
    return True


# Back-compat aliases
GruvboxDark = THEMES["gruvbox-dark"]
GruvboxLight = THEMES["gruvbox-light"]
SolarizedDark = THEMES["solarized-dark"]


def theme_css() -> str:
    """Textual CSS rules bound to the active Theme palette (full-screen light/dark)."""
    # Use concrete hex so we are not stuck on Textual $surface dark defaults
    return f"""
/* dynamic theme — injected on set_theme / startup */
Screen {{
    background: {Theme.BG};
    color: {Theme.FG};
}}
#top-bar {{
    background: {Theme.DARK1};
    color: {Theme.FG};
}}
#agent-tab-bar {{
    background: {Theme.DARK1};
    color: {Theme.FG};
}}
#agent-header {{
    background: {Theme.DARK2};
    color: {Theme.FG};
}}
VerticalScroll {{
    background: {Theme.BG};
    color: {Theme.FG};
}}
#bottom-bar {{
    background: {Theme.DARK1};
    color: {Theme.FG};
}}
#input-bar {{
    background: {Theme.BG};
    color: {Theme.FG};
    border: heavy {Theme.DARK2};
}}
#input-bar:focus {{
    border: heavy {Theme.DARK3};
}}
#dynamic-footer {{
    background: {Theme.DARK1};
    color: {Theme.GRAY};
}}
ThinkingBlock {{
    background: {Theme.DARK1};
    color: {Theme.DARK4};
    border: round {Theme.DARK3};
}}
InterjectionBlock {{
    background: {Theme.DARK1};
    border: round {Theme.BR_ORANGE};
}}
SystemReminderPanel {{
    background: {Theme.DARK1};
    color: {Theme.DARK4};
}}
ThemeSelector {{
    background: {Theme.DARK1};
    color: {Theme.FG};
    border: solid {Theme.BR_AQUA};
}}
GazeSelector {{
    background: {Theme.DARK1};
    color: {Theme.FG};
    border: solid {Theme.BR_AQUA};
}}
SlashMenu {{
    background: {Theme.DARK1};
    color: {Theme.FG};
    border: solid {Theme.BR_AQUA};
}}
"""


def apply_theme_to_app(app) -> None:
    """Push current Theme colors into the running Textual app stylesheet."""
    css = theme_css()
    # Replace previous dynamic block if present
    try:
        # Textual: stylesheet.add_source with a path-like name we can re-add
        app.stylesheet.add_source(css, path="asdaaas-dynamic-theme.tcss")
        app.stylesheet.reparse()
        app.refresh_css(animate=False)
    except Exception:
        # Fallback: set screen background directly
        try:
            app.screen.styles.background = Theme.BG
            app.screen.styles.color = Theme.FG
        except Exception:
            pass
