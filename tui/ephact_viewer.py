"""
Ephemeral Artifact Viewer widget for the TUI.

Displays pinned content from <ephact> tags in agent speech.
Per-agent artifact stacks with history navigation.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from textual.containers import Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from rich.markdown import Markdown as RichMarkdown
from rich.panel import Panel
from rich.text import Text

from ephact_parser import EphactData


@dataclass
class EphactEntry:
    """An ephact with metadata for the stack."""
    data: EphactData
    agent: str
    timestamp: float = field(default_factory=time.time)


class EphactViewer(Static):
    """Pinned artifact viewer. Shows current ephact with navigation."""

    _visible = reactive(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Per-agent stacks: agent_name -> list of EphactEntry
        self._stacks: dict[str, list[EphactEntry]] = {}
        # Current view index per agent (0 = most recent)
        self._view_index: dict[str, int] = {}
        self._active_agent: str = ""
        self._closed_by_user: set[str] = set()
        # Click regions for title bar: [(start_x, end_x, action), ...]
        self._click_regions: list[tuple[int, int, object]] = []

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
            self._visible = True
            self.display = True
            self.refresh(layout=True)

    def close(self) -> None:
        """Hide the viewer (keeps stack intact)."""
        self._closed_by_user.add(self._active_agent)
        self._visible = False
        self.display = False
        self.refresh(layout=True)

    def close_current(self) -> None:
        """Remove the currently viewed ephact from the stack."""
        agent = self._active_agent
        stack = self._stacks.get(agent, [])
        if not stack:
            return
        idx = self._view_index.get(agent, len(stack) - 1)
        stack.pop(idx)
        if not stack:
            self.close()
            return
        self._view_index[agent] = min(idx, len(stack) - 1)
        self.refresh(layout=True)

    def set_active_agent(self, agent: str) -> None:
        """Switch which agent's stack is displayed."""
        self._active_agent = agent
        if agent in self._stacks and self._stacks[agent] and agent not in self._closed_by_user:
            self._visible = True
            self.display = True
        else:
            self._visible = False
            self.display = False
        self.refresh(layout=True)

    def navigate(self, direction: int) -> None:
        """Navigate history. direction: -1 = older, +1 = newer. Wraps around."""
        agent = self._active_agent
        if agent not in self._stacks or not self._stacks[agent]:
            return
        stack = self._stacks[agent]
        idx = self._view_index.get(agent, len(stack) - 1)
        self._view_index[agent] = (idx + direction) % len(stack)
        self.refresh(layout=True)

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

    def render(self) -> Panel:
        entry = self.current_entry
        if not entry:
            return Panel("No artifacts", title="📌 Artifacts", border_style="dim")

        agent = self._active_agent
        stack = self._stacks[agent]
        idx = self._view_index.get(agent, len(stack) - 1)

        # Build title with clickable regions: ◀ Tab1│Tab2│Tab3 ▶ ✕
        title = Text()
        regions: list[tuple[int, int, object]] = []
        pos = 3  # Panel renders ╭─ (2 chars) + space before title = 3

        # ◀ button (backward)
        if len(stack) > 1:
            title.append("◀ ", style="bold cyan")
            regions.append((pos, pos + 2, ("nav", -1)))
            pos += 2
        else:
            title.append("📌 ", style="cyan")
            pos += 2

        # Tabs
        for i, e in enumerate(stack):
            label = e.data.title or e.data.type.capitalize()
            if len(label) > 12:
                label = label[:11] + "…"
            if i == idx:
                title.append(label, style="bold white on blue")
            else:
                title.append(label, style="cyan")
            regions.append((pos, pos + len(label), ("tab", i)))
            pos += len(label)
            if i < len(stack) - 1:
                title.append("│", style="dim cyan")
                pos += 1

        # ▶ button (forward)
        if len(stack) > 1:
            title.append(" ▶", style="bold cyan")
            regions.append((pos, pos + 2, ("nav", 1)))
            pos += 2

        # ✕ button (close individual)
        title.append(" ✕", style="bold red")
        regions.append((pos, pos + 2, ("close_one",)))
        pos += 2

        self._click_regions = regions

        content = entry.data.content

        return Panel(
            RichMarkdown(content),
            title=title,
            title_align="left",
            border_style="cyan",
            padding=(0, 1),
        )

    def on_click(self, event) -> None:
        """Title bar: use click regions. Content area: cycle stack."""
        if event.y == 0:
            x = event.x
            for start, end, action in self._click_regions:
                if start <= x < end:
                    if action[0] == "nav":
                        self.navigate(action[1])
                    elif action[0] == "tab":
                        self._view_index[self._active_agent] = action[1]
                        self.refresh(layout=True)
                    elif action[0] == "close_one":
                        self.close_current()
                    return
            # Clicked title bar but missed a region — hide viewer
            self.close()
            return
        # Content area click: cycle forward
        agent = self._active_agent
        stack = self._stacks.get(agent, [])
        if len(stack) > 1:
            idx = self._view_index.get(agent, len(stack) - 1)
            self._view_index[agent] = (idx + 1) % len(stack)
            self.refresh(layout=True)


def archive_ephact(agent: str, entry: EphactEntry, agents_home: str = None) -> Path:
    """Save an ephact to disk for cross-session review.

    Returns the path to the saved file.
    """
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
