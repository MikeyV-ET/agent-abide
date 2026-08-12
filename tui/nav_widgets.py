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

def layout_agent_tabs(
    tabs: list[str],
    active: str,
    width: int,
    scroll: int = 0,
    room_tab: str = "#room",
) -> dict:
    """Lay out agent tabs into a fixed terminal width without mid-tab clipping.

    Mid-tab clips rendered as black boxes when Textual/Rich truncates a styled
    segment. We only emit whole tabs (or one ellipsized tab if a single name
    is wider than the bar).

    Returns:
      segments: list of (tab_id|None, label, width, kind)
                kind in {"tab", "left_hint", "right_hint"}
      scroll: first visible tab index (may be adjusted to show active)
      indices: indices of visible tabs
      hidden_left, hidden_right: counts off-screen
    """
    if width < 4:
        width = 4
    n = len(tabs)
    if n == 0:
        return {
            "segments": [],
            "scroll": 0,
            "indices": [],
            "hidden_left": 0,
            "hidden_right": 0,
        }

    def label_for(tab: str) -> str:
        return "Room" if tab == room_tab else tab

    def tab_w(tab: str, lab: str | None = None) -> int:
        lab = label_for(tab) if lab is None else lab
        return len(lab) + 4  # "  name  " or " [name] "

    scroll = max(0, min(int(scroll), n - 1))

    def right_hint_w(hidden: int) -> int:
        if hidden <= 0:
            return 0
        return len(f"+{hidden}") + 2  # " +N "

    def pack_from(start: int, budget: int) -> tuple[list[int], list[str]]:
        """Return tab indices and labels that fully fit in budget from start."""
        idxs: list[int] = []
        labs: list[str] = []
        used = 0
        for i in range(start, n):
            lab = label_for(tabs[i])
            w = tab_w(tabs[i], lab)
            if not idxs and w > budget:
                # Single tab wider than budget — ellipsize label
                max_lab = max(1, budget - 4)
                if len(lab) > max_lab:
                    lab = lab[: max(1, max_lab - 1)] + "…"
                idxs.append(i)
                labs.append(lab)
                break
            if used + w > budget:
                break
            idxs.append(i)
            labs.append(lab)
            used += w
        return idxs, labs

    def fits_active(idxs: list[int]) -> bool:
        if not active or active not in tabs:
            return True
        return tabs.index(active) in idxs

    # If active is left of scroll, pull scroll back
    if active in tabs and tabs.index(active) < scroll:
        scroll = tabs.index(active)

    # Try packing; if active not visible, re-home scroll so active is included
    for _attempt in range(n + 1):
        left_hint = scroll > 0
        left_w = 2 if left_hint else 0  # "‹ "

        # Estimate right hidden assuming we fill the bar
        # Iterate once: pack without right reserve, measure remainder, re-pack
        budget_full = width - left_w
        idxs, labs = pack_from(scroll, budget_full)
        end = (idxs[-1] + 1) if idxs else scroll
        hidden_right = n - end
        rh = right_hint_w(hidden_right)
        if rh:
            idxs, labs = pack_from(scroll, max(4, budget_full - rh))
            end = (idxs[-1] + 1) if idxs else scroll
            hidden_right = n - end
            rh = right_hint_w(hidden_right)
            if rh:
                idxs, labs = pack_from(scroll, max(4, budget_full - rh))
                end = (idxs[-1] + 1) if idxs else scroll
                hidden_right = n - end

        if fits_active(idxs):
            break

        # Active not visible — set scroll so active is the rightmost visible tab
        ai = tabs.index(active)
        # Walk left from ai packing into (width - left - right estimates)
        # Assume left hint if ai > 0, right if ai < n-1
        left_w_est = 2 if ai > 0 else 0
        right_w_est = right_hint_w(max(0, n - ai - 1)) or (0 if ai == n - 1 else 4)
        budget = max(4, width - left_w_est - right_w_est)
        # Greedily include tabs ending at ai
        acc = 0
        start = ai
        lab_ai = label_for(tabs[ai])
        w_ai = tab_w(tabs[ai], lab_ai)
        if w_ai > budget:
            scroll = ai
            continue
        acc = w_ai
        j = ai - 1
        while j >= 0:
            w = tab_w(tabs[j])
            if acc + w > budget:
                break
            acc += w
            start = j
            j -= 1
        if start == scroll:
            # Could not improve — force active alone
            scroll = ai
            break
        scroll = start

    left_hint = scroll > 0
    left_w = 2 if left_hint else 0
    budget_full = width - left_w
    idxs, labs = pack_from(scroll, budget_full)
    end = (idxs[-1] + 1) if idxs else scroll
    hidden_right = n - end
    rh = right_hint_w(hidden_right)
    if rh:
        idxs, labs = pack_from(scroll, max(4, budget_full - rh))
        end = (idxs[-1] + 1) if idxs else scroll
        hidden_right = n - end

    segments: list[tuple] = []
    if left_hint:
        segments.append((None, "‹", 2, "left_hint"))
    for i, lab in zip(idxs, labs):
        segments.append((tabs[i], lab, tab_w(tabs[i], lab), "tab"))
    if hidden_right > 0:
        hlab = f"+{hidden_right}"
        segments.append((None, hlab, len(hlab) + 2, "right_hint"))

    return {
        "segments": segments,
        "scroll": scroll,
        "indices": idxs,
        "hidden_left": scroll,
        "hidden_right": hidden_right,
    }


class AgentTabBar(Static):
    """Tab bar showing available agents. Overflow scrolls; no mid-tab clip."""

    DEFAULT_CSS = """
    AgentTabBar {
        dock: top;
        height: 1;
        background: $surface;
        overflow-x: hidden;
        width: 100%;
    }
    """

    active_agent = reactive("")

    ROOM_TAB = "#room"

    def __init__(self, agents: list[str], **kwargs):
        super().__init__(**kwargs)
        self._agents = list(agents)
        self._tabs = list(agents) + [self.ROOM_TAB]
        self._scroll = 0
        self._layout_cache: dict | None = None

    def _width(self) -> int:
        try:
            w = self.size.width
            if w and w > 0:
                return int(w)
        except Exception:
            pass
        try:
            return max(int(self.app.size.width), 20)
        except Exception:
            return 80

    def _recompute(self) -> dict:
        layout = layout_agent_tabs(
            self._tabs,
            self.active_agent or "",
            self._width(),
            scroll=self._scroll,
            room_tab=self.ROOM_TAB,
        )
        self._scroll = layout["scroll"]
        self._layout_cache = layout
        return layout

    def watch_active_agent(self, _value: str) -> None:
        """Keep the active tab on-screen when selection changes."""
        self._layout_cache = None
        self._recompute()
        self.refresh()

    def on_resize(self, event) -> None:
        self._layout_cache = None
        self.refresh()

    def render(self) -> Text:
        layout = self._recompute()
        text = Text()
        for tab, lab, w, kind in layout["segments"]:
            if kind == "left_hint":
                text.append("‹ ", style=f"bold {Theme.BR_AQUA} on {Theme.DARK1}")
            elif kind == "right_hint":
                text.append(f" {lab} ", style=f"bold {Theme.BR_AQUA} on {Theme.DARK1}")
            elif tab == self.active_agent:
                text.append(f" [{lab}] ", style=f"bold {Theme.FG} on {Theme.DARK2}")
            else:
                text.append(f"  {lab}  ", style=f"{Theme.GRAY} on {Theme.DARK1}")
        # Pad remainder so clipped tail doesn't leave a black strip
        try:
            used = text.cell_len
            pad = self._width() - used
            if pad > 0:
                text.append(" " * pad, style=f"on {Theme.BG}")
        except Exception:
            pass
        return text

    def on_click(self, event) -> None:
        """Switch agent/room, or scroll when overflow hints are clicked."""
        layout = self._layout_cache or self._recompute()
        x = event.x
        pos = 0
        for tab, lab, w, kind in layout["segments"]:
            if x < pos + w:
                if kind == "left_hint":
                    self._scroll = max(0, self._scroll - 1)
                    self._layout_cache = None
                    self.refresh()
                    return
                if kind == "right_hint":
                    idxs = layout.get("indices") or []
                    if idxs:
                        self._scroll = min(idxs[0] + 1, len(self._tabs) - 1)
                    else:
                        self._scroll = min(self._scroll + 1, len(self._tabs) - 1)
                    self._layout_cache = None
                    self.refresh()
                    return
                if kind == "tab" and tab is not None and tab != self.active_agent:
                    self.active_agent = tab
                    if tab == self.ROOM_TAB:
                        self.app.action_switch_to_room()
                    else:
                        self.app.action_switch_agent(tab)
                return
            pos += w


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
            try:
                from theme import apply_theme_to_app
                apply_theme_to_app(self.app)
            except Exception:
                pass
            self.app.refresh(layout=True)


