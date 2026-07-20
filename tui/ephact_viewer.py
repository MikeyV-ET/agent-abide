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

    def push(self, agent: str, ephact: EphactData) -> None:
        """Add a new ephact to an agent's stack. Shows viewer only if agent is active."""
        if agent not in self._stacks:
            self._stacks[agent] = []
            self._view_index[agent] = 0

        entry = EphactEntry(data=ephact, agent=agent)
        self._stacks[agent].append(entry)
        self._view_index[agent] = len(self._stacks[agent]) - 1
        if agent == self._active_agent:
            self._visible = True
            self.display = True
            self.refresh(layout=True)

    def close(self) -> None:
        """Hide the viewer."""
        self._visible = False
        self.display = False
        self.refresh(layout=True)

    def set_active_agent(self, agent: str) -> None:
        """Switch which agent's stack is displayed."""
        self._active_agent = agent
        if agent in self._stacks and self._stacks[agent]:
            self._visible = True
            self.display = True
        else:
            self._visible = False
            self.display = False
        self.refresh(layout=True)

    def navigate(self, direction: int) -> None:
        """Navigate history. direction: -1 = older, +1 = newer."""
        agent = self._active_agent
        if agent not in self._stacks or not self._stacks[agent]:
            return
        stack = self._stacks[agent]
        idx = self._view_index.get(agent, len(stack) - 1)
        idx = max(0, min(len(stack) - 1, idx + direction))
        self._view_index[agent] = idx
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

        # Build navigation indicator
        agent = self._active_agent
        stack = self._stacks[agent]
        idx = self._view_index.get(agent, len(stack) - 1)
        nav = f" [{idx + 1}/{len(stack)}]" if len(stack) > 1 else ""

        # Title from ephact or type
        title_text = entry.data.title or entry.data.type.capitalize()
        title = f"📌 {title_text}{nav} — [x] close"

        # Render content as markdown for tables/lists
        content = entry.data.content

        return Panel(
            RichMarkdown(content),
            title=title,
            title_align="left",
            border_style="cyan",
            padding=(0, 1),
        )

    def on_click(self, event) -> None:
        """Top border click = close, content click = cycle stack."""
        if event.y == 0:
            self.close()
            return
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
