"""Navigation / room / footer / theme selector widgets."""
from __future__ import annotations

from textual.widgets import Static, OptionList
from textual.reactive import reactive
from textual.widgets.option_list import Option
from rich.text import Text

from theme import Theme, THEMES, set_theme, reload_themes

class RoomMessage(Static):
    """A single IRC channel message displayed in the room tab."""

    DEFAULT_CSS = """
    RoomMessage {
        padding: 0 1;
        margin: 0;
    }
    """

    NICK_COLORS = [
        Theme.BR_YELLOW, Theme.BR_GREEN, Theme.BR_BLUE,
        Theme.BR_PURPLE, Theme.BR_AQUA, Theme.BR_ORANGE, Theme.BR_RED,
    ]

    def __init__(self, timestamp: str, nick: str, message: str, is_action: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._timestamp = timestamp
        self._nick = nick
        self._message = message
        self._is_action = is_action

    def render(self) -> Text:
        color = self.NICK_COLORS[hash(self._nick.lower()) % len(self.NICK_COLORS)]
        ts_style = Theme.DARK4

        text = Text()
        text.append(f"{self._timestamp} ", style=ts_style)
        if self._is_action:
            text.append(f"* {self._nick} ", style=f"italic {color}")
            text.append(self._message, style=f"italic {Theme.FG}")
        else:
            text.append(f"<{self._nick}> ", style=f"bold {color}")
            text.append(self._message, style=Theme.FG)
        return text

class RoomSystemMessage(Static):
    """Join/part/quit messages in the room tab."""

    DEFAULT_CSS = """
    RoomSystemMessage {
        padding: 0 1;
        margin: 0;
    }
    """

    def __init__(self, timestamp: str, message: str, **kwargs):
        super().__init__(**kwargs)
        self._timestamp = timestamp
        self._message = message

    def render(self) -> Text:
        text = Text()
        text.append(f"{self._timestamp} ", style=Theme.DARK4)
        text.append(self._message, style=f"italic {Theme.DARK3}")
        return text

class AgentTabBar(Static):
    """Tab bar showing all available agents. Click to switch."""

    DEFAULT_CSS = """
    AgentTabBar {
        dock: top;
        height: 1;
        background: $surface;
    }
    """

    active_agent = reactive("")

    ROOM_TAB = "#room"

    def __init__(self, agents: list[str], **kwargs):
        super().__init__(**kwargs)
        self._agents = agents
        self._tabs = agents + [self.ROOM_TAB]

    def render(self) -> Text:
        text = Text()
        for tab in self._tabs:
            label = "Room" if tab == self.ROOM_TAB else tab
            if tab == self.active_agent:
                text.append(f" [{label}] ", style=f"bold {Theme.FG} on {Theme.DARK2}")
            else:
                text.append(f"  {label}  ", style=f"{Theme.GRAY} on {Theme.DARK1}")
        return text

    def on_click(self, event) -> None:
        """Switch agent/room on click by calculating which tab was clicked."""
        x = event.x
        pos = 0
        for tab in self._tabs:
            label = "Room" if tab == self.ROOM_TAB else tab
            tab_width = len(label) + 4
            if x < pos + tab_width:
                if tab != self.active_agent:
                    self.active_agent = tab
                    if tab == self.ROOM_TAB:
                        self.app.action_switch_to_room()
                    else:
                        self.app.action_switch_agent(tab)
                return
            pos += tab_width

class DynamicFooter(Static):
    """Footer that shows different keybindings based on agent state."""

    DEFAULT_CSS = """
    DynamicFooter {
        height: 1;
        background: $surface;
    }
    """

    is_generating = reactive(False)

    IDLE_BINDINGS = [
        ("^c", "Interrupt"), ("^l", "Clear"), ("^g", "Gaze"),
        ("^n", "Next Agent"), ("f1", "Thinking"), ("f2", "Persist."), ("^q", "Quit"),
    ]

    GENERATING_BINDINGS = [
        ("^c", "Interrupt"),
    ]

    def render(self) -> Text:
        text = Text()
        bindings = self.GENERATING_BINDINGS if self.is_generating else self.IDLE_BINDINGS
        for key, label in bindings:
            text.append(f" {key} ", style=f"bold {Theme.BR_ORANGE}")
            text.append(f"{label} ", style=Theme.FG)
        return text

class ThemeSelector(OptionList):
    """Dropdown overlay for selecting color theme.

    Each row shows a live color sample (swatches + mini UI line) so you can
    see what the theme does before applying it.
    """

    DEFAULT_CSS = """
    ThemeSelector {
        layer: overlay;
        dock: top;
        margin: 2 0 0 0;
        width: 56;
        max-height: 16;
        border: solid $accent;
        background: $surface;
        display: none;
        offset-x: 30;
    }
    """

    def on_blur(self, event) -> None:
        self.display = False

    @staticmethod
    def _preview_line(palette, name: str, current: bool) -> Text:
        """Build a one-line visual sample of the palette."""
        bg = getattr(palette, "BG", "#000000")
        fg = getattr(palette, "FG", "#ffffff")
        gray = getattr(palette, "GRAY", "#888888")
        aqua = getattr(palette, "BR_AQUA", "#00ffff")
        green = getattr(palette, "BR_GREEN", "#00ff00")
        orange = getattr(palette, "BR_ORANGE", "#ff8800")
        red = getattr(palette, "BR_RED", "#ff0000")
        yellow = getattr(palette, "BR_YELLOW", "#ffff00")
        blue = getattr(palette, "BR_BLUE", "#0088ff")

        line = Text()
        # Color chips
        for color in (green, aqua, orange, red, yellow, blue):
            line.append("█", style=color)
        line.append(" ")
        # Mini chrome: user chevron + speech + tool + dim
        line.append("❯", style=f"bold {blue}")
        line.append(" you ", style=fg)
        line.append("speech ", style=fg)
        line.append("🔧", style=aqua)
        line.append(" tool ", style=gray)
        line.append("warn", style=orange)
        line.append(" ")
        # Theme name on its own bg so you see surface contrast
        mark = " *" if current else ""
        line.append(f" {name}{mark} ", style=f"{fg} on {bg}")
        return line

    def populate(self) -> None:
        """Refresh options with per-theme color previews + Auto (system)."""
        reload_themes()
        from theme import (
            Theme as T, THEMES as themes_map, resolve_theme_key,
            detect_system_appearance, load_theme_config,
        )
        self.clear_options()
        self.border_title = "Themes — preview · Enter to apply · Esc"
        pref = T.preference() if hasattr(T, "preference") else getattr(T, "_key", None)
        # Auto first
        appearance = detect_system_appearance()
        resolved = resolve_theme_key("auto")
        pal = themes_map.get(resolved)
        auto_current = str(pref).lower() in ("auto", "system")
        if pal is not None:
            label = f"Auto (system → {appearance} → {getattr(pal, 'NAME', resolved)})"
            preview = self._preview_line(pal, label, auto_current)
            self.add_option(Option(preview, id="auto"))
        cur_key = getattr(T, "_key", None)
        for key, palette in themes_map.items():
            name = getattr(palette, "NAME", key)
            current = (not auto_current) and (key == cur_key or key == pref)
            preview = self._preview_line(palette, name, current)
            self.add_option(Option(preview, id=key))

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Show which theme is focused in the border subtitle."""
        opt = event.option
        if opt and opt.id:
            from theme import THEMES as themes_map
            pal = themes_map.get(opt.id)
            name = getattr(pal, "NAME", opt.id) if pal else opt.id
            self.border_subtitle = f"preview: {name}"

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Apply selected theme."""
        theme_key = event.option.id
        if theme_key and set_theme(theme_key):
            self.display = False
            self.border_subtitle = ""
            self.app.refresh(layout=True)


