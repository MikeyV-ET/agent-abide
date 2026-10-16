"""
Ephemeral Artifact Viewer widget for the TUI.

Displays pinned content from <ephact> tags in agent speech.
Per-agent artifact stacks with history navigation.
Body is a VerticalScroll so long artifacts can be scrolled.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static

from rich.markdown import Markdown as RichMarkdown
from rich.text import Text

from ephact_parser import EphactData
from theme import Theme


@dataclass
class EphactEntry:
    """An ephact with metadata for the stack."""
    data: EphactData
    agent: str
    timestamp: float = field(default_factory=time.time)


class EphactViewer(Vertical):
    """Pinned artifact viewer with scrollable body and tab navigation."""

    DEFAULT_CSS = """
    EphactViewer {
        /* Fixed fraction so #ephact-scroll (1fr) gets a real viewport to scroll */
        height: 40%;
        max-height: 50%;
        min-height: 10;
        border: solid cyan;
        background: transparent;
        padding: 0 0;
    }
    EphactViewer #ephact-title {
        height: 1;
        padding: 0 1;
        background: transparent;
    }
    EphactViewer #ephact-scroll {
        height: 1fr;
        max-height: 100%;
        min-height: 3;
        overflow-y: auto;
        overflow-x: hidden;
        scrollbar-size: 1 1;
        padding: 0 1;
    }
    EphactViewer #ephact-body {
        height: auto;
        padding: 0 0 1 0;
    }
    EphactViewer #ephact-tabs {
        height: 1;
        padding: 0 1;
        background: transparent;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._stacks: dict[str, list[EphactEntry]] = {}
        self._view_index: dict[str, int] = {}
        self._active_agent: str = ""
        self._closed_by_user: set[str] = set()
        self._click_regions: list[tuple[int, int, object]] = []
        self._tab_scroll: dict[str, int] = {}
        self.display = False

    # Back-compat for tests that used reactive _visible
    @property
    def _visible(self) -> bool:
        return bool(self.display)

    @_visible.setter
    def _visible(self, value: bool) -> None:
        self.display = bool(value)


    def compose(self) -> ComposeResult:
        yield Static(id="ephact-title")
        with VerticalScroll(id="ephact-scroll"):
            yield Static(id="ephact-body")
        yield Static(id="ephact-tabs")

    def on_mount(self) -> None:
        self._refresh_display()

    def push(self, agent: str, ephact: EphactData) -> None:
        """Add a new ephact to an agent's stack. Shows viewer only if agent is active."""
        if agent not in self._stacks:
            self._stacks[agent] = []
            self._view_index[agent] = 0

        entry = EphactEntry(data=ephact, agent=agent)
        self._stacks[agent].append(entry)
        self._view_index[agent] = len(self._stacks[agent]) - 1
        self._closed_by_user.discard(agent)
        if agent == self._active_agent:
            self.display = True
            self._refresh_display()
            # Jump body scroll to top for new content
            try:
                self.query_one("#ephact-scroll", VerticalScroll).scroll_home(animate=False)
            except Exception:
                pass

    def close(self) -> None:
        """Hide the viewer (keeps stack intact)."""
        self._closed_by_user.add(self._active_agent)
        self.display = False
        self.refresh(layout=True)

    def close_current(self) -> None:
        """Remove the currently viewed ephact from the stack."""
        agent = self._active_agent
        idx = self._view_index.get(agent, 0)
        self.close_at(idx)

    def close_at(self, index: int) -> None:
        """Remove the ephact at the given index from the stack."""
        agent = self._active_agent
        stack = self._stacks.get(agent, [])
        if not stack or index < 0 or index >= len(stack):
            return
        stack.pop(index)
        if not stack:
            self.close()
            return
        cur = self._view_index.get(agent, 0)
        if cur >= len(stack):
            self._view_index[agent] = len(stack) - 1
        elif index < cur:
            self._view_index[agent] = cur - 1
        self._refresh_display()

    def set_active_agent(self, agent: str) -> None:
        """Switch which agent's stack is displayed."""
        self._active_agent = agent
        if agent in self._stacks and self._stacks[agent] and agent not in self._closed_by_user:
            self.display = True
        else:
            self.display = False
        self._refresh_display()

    def navigate(self, direction: int) -> None:
        """Navigate history. direction: -1 = older, +1 = newer. Wraps around."""
        agent = self._active_agent
        if agent not in self._stacks or not self._stacks[agent]:
            return
        stack = self._stacks[agent]
        idx = self._view_index.get(agent, len(stack) - 1)
        self._view_index[agent] = (idx + direction) % len(stack)
        self._refresh_display()
        try:
            self.query_one("#ephact-scroll", VerticalScroll).scroll_home(animate=False)
        except Exception:
            pass

    @property
    def has_content(self) -> bool:
        agent = self._active_agent
        return bool(self._stacks.get(agent))

    @property
    def current_entry(self) -> Optional[EphactEntry]:
        agent = self._active_agent
        stack = self._stacks.get(agent, [])
        if not stack:
            return None
        idx = self._view_index.get(agent, len(stack) - 1)
        return stack[idx]

    def _render_body_text(self, content: str) -> Text:
        from io import StringIO
        from rich.console import Console as RichConsole
        from rich.style import Style as RichStyle
        buf = StringIO()
        w = self.size.width - 6 if self.size.width > 12 else 80
        console = RichConsole(file=buf, force_terminal=True, width=w, no_color=False)
        console.print(RichMarkdown(content), end="")
        rendered = Text.from_ansi(buf.getvalue())
        rlines = rendered.split("\n")
        for rline in rlines:
            plain = rline.plain
            slen = len(plain.rstrip())
            if slen < len(plain):
                has_bg = any(
                    end > slen and RichStyle.parse(str(s)).bgcolor
                    for start, end, s in rline._spans
                )
                if not has_bg:
                    rline.rstrip()
        return Text("\n").join(rlines)

    def _build_tabs(self) -> Text:
        agent = self._active_agent
        stack = self._stacks.get(agent, [])
        if not stack:
            return Text("")
        idx = self._view_index.get(agent, len(stack) - 1)
        subtitle = Text()
        regions: list[tuple[int, int, object]] = []
        left_pos = 1
        panel_width = self.size.width if self.size.width > 20 else 80
        arrow_width = 2
        avail = panel_width - 6 - (arrow_width * 2)

        tab_labels = []
        for i, e in enumerate(stack):
            label = e.data.title or e.data.type.capitalize()
            if len(label) > 12:
                label = label[:11] + "…"
            w = len(label) + 2 + (1 if i < len(stack) - 1 else 0)
            tab_labels.append((i, label, w))

        scroll = self._tab_scroll.get(agent, 0)
        scroll = max(0, min(scroll, len(stack) - 1))
        visible_end = scroll
        used_width = 0
        for ti, label, w in tab_labels[scroll:]:
            if used_width + w > avail:
                break
            used_width += w
            visible_end = ti + 1

        if idx < scroll:
            self._view_index[agent] = scroll
            idx = scroll
        elif idx >= visible_end:
            self._view_index[agent] = max(scroll, visible_end - 1)
            idx = self._view_index[agent]

        self._tab_scroll[agent] = scroll
        has_left = scroll > 0
        has_right = visible_end < len(stack)

        pos = left_pos
        if has_left:
            subtitle.append("◀ ", style="bold cyan")
            regions.append((pos, pos + arrow_width, ("scroll", -1)))
        else:
            subtitle.append("  ", style="dim")
        pos += arrow_width

        for ti, label, w in tab_labels[scroll:visible_end]:
            if ti == idx:
                subtitle.append(label, style="bold white on blue")
            else:
                subtitle.append(label, style="cyan")
            regions.append((pos, pos + len(label), ("tab", ti)))
            pos += len(label)
            subtitle.append(" ×", style="bold red")
            regions.append((pos, pos + 2, ("close_one", ti)))
            pos += 2
            if ti < visible_end - 1:
                subtitle.append("│", style="dim cyan")
                pos += 1

        right_pos = left_pos + arrow_width + avail
        pad = right_pos - pos
        if pad > 0:
            subtitle.append(" " * pad)
            pos = right_pos

        if has_right:
            subtitle.append(" ▶", style="bold cyan")
            regions.append((pos, pos + arrow_width, ("scroll", 1)))
        else:
            subtitle.append("  ", style="dim")

        self._click_regions = regions
        return subtitle

    def _refresh_display(self) -> None:
        """Update title / body / tabs children from current entry."""
        try:
            title_w = self.query_one("#ephact-title", Static)
            body_w = self.query_one("#ephact-body", Static)
            tabs_w = self.query_one("#ephact-tabs", Static)
        except Exception:
            return

        entry = self.current_entry
        if not entry or not self.display:
            title_w.update(Text(""))
            body_w.update(Text(""))
            tabs_w.update(Text(""))
            return

        title_text = entry.data.title or entry.data.type.capitalize()
        title_w.update(Text(f"📌 {title_text}  [click title to close]", style=f"bold {Theme.BR_AQUA}"))
        body_w.update(self._render_body_text(entry.data.content))
        tabs_w.update(self._build_tabs())
        self.refresh(layout=True)

    def on_click(self, event) -> None:
        """Title: close. Tabs: navigate/close. Body clicks bubble to scroll."""
        if getattr(event, "button", 1) != 1:
            return
        # Map click to child region roughly by y
        try:
            title_w = self.query_one("#ephact-title", Static)
            tabs_w = self.query_one("#ephact-tabs", Static)
            scroll_w = self.query_one("#ephact-scroll", VerticalScroll)
        except Exception:
            return

        # Clicks on title bar
        if event.widget is title_w or (
            hasattr(event, "y") and event.y == 0 and event.widget is self
        ):
            # Only close if click is on the title strip
            if event.widget is title_w:
                self.close()
                event.stop()
                return

        # Tab bar clicks
        if event.widget is tabs_w:
            x = event.x
            for start, end, action in self._click_regions:
                if start <= x < end:
                    if action[0] == "scroll":
                        agent = self._active_agent
                        sc = self._tab_scroll.get(agent, 0)
                        self._tab_scroll[agent] = max(0, sc + action[1])
                        self._refresh_display()
                    elif action[0] == "tab":
                        self._view_index[self._active_agent] = action[1]
                        self._refresh_display()
                        try:
                            scroll_w.scroll_home(animate=False)
                        except Exception:
                            pass
                    elif action[0] == "close_one":
                        self.close_at(action[1])
                    event.stop()
                    return
            event.stop()
            return

        # Body: leave for scroll; optional cycle on empty area of body widget only if not dragging
        # (do not cycle on body click — that fights scroll selection)


def archive_ephact(
    agent: str,
    entry: EphactEntry,
    agents_home: str = None,
    agent_home: str | Path = None,
) -> Path:
    """Save an ephact to disk for cross-session review.

    Prefer ``agent_home`` (full agent directory, e.g. from Config.agent_home /
    agents.json ``home``). If only ``agents_home`` is given, writes to
    ``{agents_home}/{agent}/ephacts`` (flat layout / tests).

    Returns the path to the saved file.
    """
    if agent_home is not None:
        ephacts_dir = Path(agent_home) / "ephacts"
    else:
        if agents_home is None:
            agents_home = str(Path.home() / "agents")
        ephacts_dir = Path(agents_home) / agent / "ephacts"
    ephacts_dir.mkdir(parents=True, exist_ok=True)

    ts = int(entry.timestamp * 1000)
    path = ephacts_dir / f"ephact_{ts}.json"
    data = {
        "type": entry.data.type,
        "title": entry.data.title,
        "content": entry.data.content,
        "agent": entry.agent,
        "timestamp": entry.timestamp,
    }
    path.write_text(json.dumps(data, indent=2))
    return path
