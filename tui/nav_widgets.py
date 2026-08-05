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
    """Dropdown overlay for selecting color theme."""

    DEFAULT_CSS = """
    ThemeSelector {
        layer: overlay;
        dock: top;
        margin: 2 0 0 0;
        width: 30;
        max-height: 10;
        border: solid $accent;
        background: $surface;
        display: none;
        offset-x: 50;
    }
    """

    def on_blur(self, event) -> None:
        self.display = False

    def populate(self) -> None:
        """Refresh the option list with available themes."""
        # Pick up new JSON files dropped in tui/themes/
        from theme import THEMES as themes_map
        reload_themes()
        self.clear_options()
        for key, palette in themes_map.items():
            # after reload, Theme.current() is palette object
            from theme import Theme as T, THEMES as TM
            current = " *" if key == getattr(T, "_key", None) or palette is T.current() else ""
            name = getattr(palette, "NAME", key)
            self.add_option(Option(f"  {name}{current}", id=key))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Apply selected theme."""
        theme_key = event.option.id
        if theme_key and set_theme(theme_key):
            self.display = False
            self.app.refresh(layout=True)


