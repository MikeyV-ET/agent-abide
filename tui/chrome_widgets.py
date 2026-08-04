"""Chrome widgets: scroll, alerts, turn separators."""
from __future__ import annotations

from textual.containers import VerticalScroll
from textual.widgets import Static
from rich.text import Text

from theme import Theme

def classify_turn_trigger(text: str) -> str:
    """Classify a user_message_chunk's content into a human-readable trigger label."""
    t = text.strip()
    # asdaaas doorbells
    if "[continue" in t:
        return "continue"
    if "[context" in t and "%" in t:
        # Extract percentage
        import re
        m = re.search(r'(\d+)%', t)
        pct = m.group(1) if m else "?"
        return f"context {pct}%"
    if "[heartbeat" in t or "heartbeat" in t.lower()[:50]:
        return "heartbeat"
    if "[session:compact" in t:
        return "compact"
    if "[Compaction complete" in t:
        return "compaction done"
    # localmail
    if "localmail" in t.lower()[:60] or "[FROM:" in t[:30]:
        import re
        m = re.search(r'from[:\s]+(\w+)', t[:80], re.IGNORECASE)
        src = m.group(1) if m else "?"
        return f"mail from {src}"
    # Eric via TUI
    if "<eric" in t.lower()[:30] or "(via tui)" in t.lower()[:50]:
        return "Eric (tui)"
    # IRC message
    if "irc" in t.lower()[:30] or "#" in t[:20]:
        return "IRC"
    # Generic — show first 30 chars
    preview = t[:30].replace("\n", " ")
    if len(t) > 30:
        preview += "..."
    return preview

class SystemAlert(Static):
    """System notification bar for retry, doom loop, compaction events."""

    def __init__(self, message: str, severity: str = "warning", **kwargs):
        super().__init__(**kwargs)
        self.alert_message = message
        self.severity = severity

    def render(self) -> Text:
        text = Text()
        if self.severity == "error":
            text.append(" ⚠ ", style=f"bold {Theme.BR_RED}")
            text.append(self.alert_message, style=Theme.BR_RED)
        elif self.severity == "warning":
            text.append(" ⚠ ", style=f"bold {Theme.BR_YELLOW}")
            text.append(self.alert_message, style=Theme.BR_YELLOW)
        else:
            text.append(" ℹ ", style=f"bold {Theme.BR_BLUE}")
            text.append(self.alert_message, style=Theme.BR_BLUE)
        return text

class ContentScroll(VerticalScroll):
    """VerticalScroll for agent content. Auto-loads history on mouse scroll at top."""

    _follow_tail: bool = True

    def on_mount(self) -> None:
        self.auto_scroll = False

    def on_mouse_scroll_up(self, event) -> None:
        """When user scrolls up, disable auto-follow and load history at top."""
        self._follow_tail = False
        if self.scroll_y <= 0:
            try:
                self.app._load_older_history()
            except Exception:
                pass

    def on_mouse_scroll_down(self, event) -> None:
        """When user scrolls down, check if we've reached the bottom."""
        self.set_timer(0.1, self._check_at_bottom)

    def on_key(self, event) -> None:
        """Track keyboard scrolling."""
        if event.key in ("up", "pageup", "home"):
            self._follow_tail = False
        elif event.key in ("down", "pagedown"):
            self.set_timer(0.1, self._check_at_bottom)

    def _check_at_bottom(self) -> None:
        """Re-enable auto-follow when scrolled to bottom."""
        if self.max_scroll_y > 0 and self.scroll_y >= self.max_scroll_y - 2:
            self._follow_tail = True

class HookAnnotation(Static):
    """Dimmed status line for hook annotations."""

    def __init__(self, message: str, **kwargs):
        super().__init__(**kwargs)
        self.annotation_message = message

    def render(self) -> Text:
        return Text(f"  {self.annotation_message}", style=f"italic {Theme.DARK4}")

class TurnSeparator(Static):
    """Visual separator between logical turns showing turn number and trigger."""

    def __init__(self, turn_num: int, trigger: str, timestamp: str = "", **kwargs):
        super().__init__(**kwargs)
        self._turn_num = turn_num
        self._trigger = trigger
        self._timestamp = timestamp

    def render(self) -> Text:
        text = Text()
        # Thin horizontal rule with turn info
        text.append(" T", style=f"bold {Theme.DARK4}")
        text.append(f"{self._turn_num}", style=f"bold {Theme.BR_AQUA}")
        text.append(f" {self._trigger}", style=Theme.DARK4)
        if self._timestamp:
            text.append(f"  {self._timestamp}", style=Theme.DARK3)
        # Fill remaining width with thin line
        pad = max(0, 60 - len(text.plain))
        text.append(" " + "\u2500" * pad, style=Theme.DARK3)
        return text


