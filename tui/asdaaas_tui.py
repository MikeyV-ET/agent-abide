#!/usr/bin/env python3
"""
asdaaas_tui.py — Full-screen Textual TUI for asdaaas agent sessions.

Phase 1: Replicate the grok TUI development experience, routed through asdaaas.
The human operator should not be able to tell the difference from the real grok TUI.

Architecture:
  Input:  User types in InputBar → written to asdaaas TUI adapter inbox as JSON
  Output: Tails updates.jsonl from the agent's session → renders events in real-time
  Status: Polls health.json + gaze.json for the status bar

Layout:
  ┌─────────────────────────────────────────────┐
  │  Header: Agent Name │ Context: 56% │ Gaze   │
  ├─────────────────────────────────────────────┤
  │                                             │
  │  [Scrollable content area]                  │
  │  - Agent messages (markdown)                │
  │  - Tool call panels (bordered boxes)        │
  │  - Thinking blocks (dimmed/collapsible)     │
  │  - Plan/todo updates                        │
  │  - User messages                            │
  │                                             │
  ├─────────────────────────────────────────────┤
  │  > [Input bar]                              │
  ├─────────────────────────────────────────────┤
  │  Footer: keybindings                        │
  └─────────────────────────────────────────────┘
"""

import argparse
import asyncio
import datetime
import json
import os
import sys
import time
import secrets
import threading
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll, Center
from textual.screen import ModalScreen
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widgets import Header, Input, Static, RichLog, Collapsible, OptionList, TextArea
from textual.widgets.option_list import Option
from textual.worker import Worker, get_current_worker
from textual import work

from rich.markdown import Markdown as RichMarkdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text
from rich.table import Table
from rich.console import Group


# =============================================================================
# Color Palette — Gruvbox Dark
# =============================================================================

class Gruvbox:
    """Gruvbox dark mode color palette."""
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
    # Bright variants
    BR_RED = "#fb4934"
    BR_GREEN = "#b8bb26"
    BR_YELLOW = "#fabd2f"
    BR_BLUE = "#83a598"
    BR_PURPLE = "#d3869b"
    BR_AQUA = "#8ec07c"
    BR_ORANGE = "#fe8019"
    # Darks
    DARK1 = "#3c3836"
    DARK2 = "#504945"
    DARK3 = "#665c54"
    DARK4 = "#7c6f64"


# =============================================================================
# Configuration
# =============================================================================

class Config:
    """Runtime configuration, set from CLI args."""
    AGENT_NAME: str = "Trip"
    AGENTS_HOME: str = os.path.expanduser("~/agents")
    GROK_SESSIONS_DIR: Optional[str] = None  # Override for session directory
    SESSION_DIR: Optional[str] = None  # Auto-detected from sessions root
    UPDATES_FILE: Optional[str] = None  # Path to updates.jsonl
    API_URL: Optional[str] = None  # asdaaas API base URL (e.g. http://localhost:8420)
    _agents_cfg: dict = {}  # Cached agents.json content

    @classmethod
    def load_agents_cfg(cls) -> dict:
        """Load and cache agents.json."""
        if not cls._agents_cfg:
            try:
                p = Path.home() / "projects" / "agent-abide" / "agents.json"
                with open(p) as f:
                    cls._agents_cfg = json.load(f)
            except Exception:
                cls._agents_cfg = {}
        return cls._agents_cfg

    @classmethod
    def agent_backend(cls, agent_name: str) -> str:
        """Return backend type for an agent ('grok' or 'claude')."""
        cfg = cls.load_agents_cfg()
        agent_cfg = cfg.get("agents", {}).get(agent_name, {})
        return agent_cfg.get("backend", "grok")

    @classmethod
    def sessions_root(cls) -> Path:
        """Get the grok sessions root directory."""
        if cls.GROK_SESSIONS_DIR:
            return Path(cls.GROK_SESSIONS_DIR)
        try:
            from asdaaas_config import config as asdaaas_cfg
            return asdaaas_cfg.grok_sessions_dir
        except ImportError:
            pass
        # Fallback: auto-detect
        standard = Path.home() / ".grok" / "sessions"
        if standard.exists():
            return standard
        grok_users = Path.home() / ".grok-users"
        if grok_users.exists():
            for user_dir in grok_users.iterdir():
                candidate = user_dir / ".grok" / "sessions"
                if candidate.exists():
                    return candidate
        return standard

    OPERATOR_NAME: Optional[str] = None  # Who is using this TUI
    OPERATOR_FILE: Path = Path.home() / ".config" / "abidetui" / "operator.json"

    @classmethod
    def load_operator(cls) -> Optional[str]:
        """Load saved operator name."""
        try:
            with open(cls.OPERATOR_FILE) as f:
                data = json.load(f)
            return data.get("name")
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    @classmethod
    def save_operator(cls, name: str):
        """Save operator name to disk."""
        cls.OPERATOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(cls.OPERATOR_FILE, "w") as f:
            json.dump({"name": name}, f)

    @classmethod
    def agent_dir(cls) -> Path:
        return Path(cls.AGENTS_HOME) / cls.AGENT_NAME

    @classmethod
    def asdaaas_dir(cls) -> Path:
        return cls.agent_dir() / "asdaaas"

    @classmethod
    def health_file(cls) -> Path:
        return cls.asdaaas_dir() / "health.json"

    @classmethod
    def gaze_file(cls) -> Path:
        return cls.asdaaas_dir() / "gaze.json"

    @classmethod
    def awareness_file(cls) -> Path:
        return cls.asdaaas_dir() / "awareness.json"

    @classmethod
    def tui_inbox(cls) -> Path:
        return cls.asdaaas_dir() / "adapters" / "tui" / "inbox"

    @classmethod
    def tui_outbox(cls) -> Path:
        return cls.asdaaas_dir() / "adapters" / "tui" / "outbox"

    @classmethod
    def write_command(cls, cmd: dict) -> None:
        """Write a command to the agent's asdaaas command queue."""
        import secrets as _secrets
        cmd_dir = cls.asdaaas_dir() / "commands"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        rand = _secrets.token_hex(4)
        with open(cmd_dir / f"cmd_{ts}_{rand}.json", "w") as f:
            json.dump(cmd, f)

    @classmethod
    def find_updates_file(cls) -> Optional[Path]:
        """Find the updates.jsonl for this agent's session."""
        if cls.UPDATES_FILE:
            return Path(cls.UPDATES_FILE)
        sessions_root = cls.sessions_root()
        agent_path = cls.agent_dir()
        encoded = str(agent_path).replace("/", "%2F")
        session_dir = sessions_root / encoded
        if session_dir.exists():
            # Find the session subdirectory (UUID)
            subdirs = [d for d in session_dir.iterdir() if d.is_dir()]
            if subdirs:
                # Use the most recent one
                latest = max(subdirs, key=lambda d: d.stat().st_mtime)
                updates = latest / "updates.jsonl"
                if updates.exists():
                    return updates
        return None

    @classmethod
    def find_signals_file(cls) -> Optional[Path]:
        """Find signals.json for context window info."""
        sessions_root = cls.sessions_root()
        agent_path = cls.agent_dir()
        encoded = str(agent_path).replace("/", "%2F")
        session_dir = sessions_root / encoded
        if session_dir.exists():
            subdirs = [d for d in session_dir.iterdir() if d.is_dir()]
            if subdirs:
                latest = max(subdirs, key=lambda d: d.stat().st_mtime)
                signals = latest / "signals.json"
                if signals.exists():
                    return signals
        return None


# =============================================================================
# Custom Widgets
# =============================================================================

class AgentHeader(Static):
    """Status bar showing agent name, context usage, gaze target, health.
    
    The gaze field is clickable — clicking it opens a dropdown to change gaze.
    """

    agent_name = reactive("Agent")
    context_pct = reactive(0)
    gaze_target = reactive("unknown")
    health_status = reactive("unknown")
    compaction_count = reactive(0)
    compaction_phase = reactive("")
    compaction_detail = reactive("")
    model_name = reactive("")
    is_generating = reactive(False)
    turn_physical = reactive(0)
    turn_logical = reactive(0)
    delay_pattern = reactive("")
    code_version = reactive("")
    code_version_stale = reactive(False)

    def render(self) -> Text:
        text = Text()
        # Agent name
        text.append(f" {self.agent_name} ", style=f"bold {Gruvbox.FG} on {Gruvbox.DARK2}")
        text.append("  ")

        # Context usage with color coding
        pct = self.context_pct
        if pct < 50:
            ctx_style = Gruvbox.BR_GREEN
        elif pct < 70:
            ctx_style = Gruvbox.BR_YELLOW
        elif pct < 85:
            ctx_style = Gruvbox.BR_ORANGE
        else:
            ctx_style = f"bold {Gruvbox.BR_RED}"
        text.append("ctx: ", style=Gruvbox.GRAY)
        text.append(f"{pct}%", style=ctx_style)

        if self.compaction_count > 0:
            text.append(f" (c:{self.compaction_count})", style=Gruvbox.GRAY)

        # Compaction phase indicator
        cp = self.compaction_phase
        if cp in ("in_flight", "pending"):
            text.append(" ⟳ compacting", style=f"bold {Gruvbox.BR_ORANGE}")
        elif cp == "complete" and self.compaction_detail:
            text.append(f" ✓ compacted {self.compaction_detail}", style=Gruvbox.BR_GREEN)
        elif cp == "complete":
            text.append(" ✓ compacted", style=Gruvbox.BR_GREEN)
        elif cp == "failed":
            text.append(" ✗ compact fail", style=f"bold {Gruvbox.BR_RED}")

        text.append("  ")

        # Gaze (clickable)
        text.append("gaze: ", style=Gruvbox.GRAY)
        text.append(f"[{self.gaze_target}]", style=f"bold underline {Gruvbox.BR_AQUA}")
        text.append(" ▾", style=Gruvbox.GRAY)
        text.append("  ")

        # Health + spinner
        h = self.health_status
        if self.is_generating:
            # Braille spinner animation
            spinner_frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            import time as _time
            frame = spinner_frames[int(_time.time() * 8) % len(spinner_frames)]
            text.append(frame, style=f"bold {Gruvbox.BR_AQUA}")
            text.append(" generating", style=Gruvbox.GRAY)
        elif h == "working":
            text.append("●", style=Gruvbox.BR_GREEN)
            text.append(f" {h}", style=Gruvbox.GRAY)
        elif h in ("ready", "active"):
            text.append("●", style=Gruvbox.BR_AQUA)
            text.append(f" {h}", style=Gruvbox.GRAY)
        elif h == "idle":
            text.append("○", style=Gruvbox.BR_YELLOW)
            text.append(f" {h}", style=Gruvbox.GRAY)
        elif h == "stalled":
            text.append("⚠", style=f"bold {Gruvbox.BR_RED}")
            text.append(f" {h}", style=Gruvbox.BR_RED)
        elif h == "restarting":
            text.append("↻", style=Gruvbox.BR_YELLOW)
            text.append(f" {h}", style=Gruvbox.GRAY)
        elif h == "error":
            text.append("✗", style=f"bold {Gruvbox.BR_RED}")
            text.append(f" {h}", style=Gruvbox.BR_RED)
        elif h in ("shutdown", "waiting"):
            text.append("◌", style=Gruvbox.GRAY)
            text.append(f" {h}", style=Gruvbox.GRAY)
        else:
            text.append("?", style=Gruvbox.BR_RED)
            text.append(f" {h}", style=Gruvbox.GRAY)

        # Turn reporting (physical turns = asdaaas-level prompt deliveries)
        if self.turn_physical > 0:
            text.append("  ")
            text.append("t:", style=Gruvbox.GRAY)
            text.append(f"{self.turn_physical}", style=Gruvbox.BR_AQUA)

        # Delay pattern
        if self.delay_pattern:
            text.append(" ")
            text.append(f"[{self.delay_pattern}]", style=Gruvbox.DARK4)

        # Model name (right side)
        if self.model_name:
            text.append("  ")
            text.append(self.model_name, style=Gruvbox.DARK4)

        # Code version
        if self.code_version:
            text.append("  ")
            if self.code_version_stale:
                text.append(f"⚠ {self.code_version}", style=f"bold {Gruvbox.BR_YELLOW}")
            else:
                text.append(self.code_version, style=Gruvbox.DARK4)

        return text

    def watch_is_generating(self, generating: bool) -> None:
        """Start/stop spinner refresh timer."""
        if generating:
            self._spinner_timer = self.set_interval(1 / 8, self.refresh)
        else:
            if hasattr(self, "_spinner_timer") and self._spinner_timer:
                self._spinner_timer.stop()
                self._spinner_timer = None
            self.refresh()

    def on_click(self, event) -> None:
        """Open gaze selector dropdown when header is clicked."""
        self.app.action_toggle_gaze_selector()


class GazeSelector(OptionList):
    """Dropdown overlay for selecting gaze target."""

    DEFAULT_CSS = """
    GazeSelector {
        layer: overlay;
        dock: top;
        margin: 2 0 0 0;
        width: 40;
        max-height: 12;
        border: solid $accent;
        background: $surface;
        display: none;
        offset-x: 20;
    }
    """

    def on_blur(self, event) -> None:
        """Dismiss when focus leaves the selector (click outside)."""
        self.display = False

    def _get_available_rooms(self) -> list[str]:
        """Build list of available gaze targets from adapters dir + awareness."""
        rooms = []

        # Discover registered adapters from filesystem
        adapters_dir = Config.asdaaas_dir() / "adapters"
        if adapters_dir.exists():
            for d in sorted(adapters_dir.iterdir()):
                if d.is_dir() and d.name in ("tui", "irc"):
                    rooms.append(d.name)

        # Read awareness for background_channels (IRC rooms, PMs)
        try:
            with open(Config.awareness_file()) as f:
                awareness = json.load(f)
            for room in awareness.get("background_channels", {}):
                if room not in rooms:
                    rooms.append(room)
        except Exception:
            pass

        # Common IRC targets for this agent
        agent = Config.AGENT_NAME.lower()
        for r in [f"pm:eric", f"#{agent}-thoughts", "#standup"]:
            if r not in rooms:
                rooms.append(r)

        # Read current gaze — make sure it's in the list
        try:
            with open(Config.gaze_file()) as f:
                gaze = json.load(f)
            speech = gaze.get("speech", {})
            target = speech.get("target", "")
            params = speech.get("params", {})
            room = params.get("room", "")
            current = room if room else target
            if current not in rooms:
                rooms.insert(0, current)
        except Exception:
            pass

        return rooms

    def populate(self) -> None:
        """Refresh the option list with available rooms."""
        self.clear_options()
        rooms = self._get_available_rooms()
        for room in rooms:
            # Determine adapter from room name
            if room == "tui":
                label = f"  tui          (direct)"
            elif room.startswith("#"):
                label = f"  irc/{room}"
            elif room.startswith("pm:"):
                label = f"  irc/{room}"
            else:
                label = f"  {room}"
            self.add_option(Option(label, id=room))
        # Add custom entry option at the end
        self.add_option(Option("  ✏️  Type custom room...", id="__custom__"))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Handle gaze selection."""
        room = event.option.id
        if room is None:
            return

        if room == "__custom__":
            self.display = False
            # Put a prompt in the input bar for the user to type a room name
            try:
                input_bar = self.app.query_one("#input-bar", MessageInput)
                input_bar.clear()
                input_bar.insert("/gaze ")
                input_bar.focus()
            except NoMatches:
                pass
            return

        # Write gaze.json directly (command queue gaze action not yet implemented in asdaaas)
        agent_lower = Config.AGENT_NAME.lower()
        if room == "tui":
            try:
                self.app._ensure_adapter_attached("tui")
            except Exception:
                pass
            gaze = {
                "speech": {"target": "tui", "params": {}},
                "thoughts": {"target": "irc", "params": {"room": f"#{agent_lower}-thoughts"}}
            }
            gaze_str = "tui"
        elif room.startswith("pm:"):
            gaze = {
                "speech": {"target": "irc", "params": {"room": room}},
                "thoughts": {"target": "irc", "params": {"room": f"#{agent_lower}-thoughts"}}
            }
            gaze_str = f"irc/{room}"
        else:
            # Check if room matches a known non-IRC adapter
            adapter_dir = Path(Config.AGENTS_HOME) / Config.AGENT_NAME / "asdaaas" / "adapters" / room
            if adapter_dir.exists() and room != "irc":
                try:
                    self.app._ensure_adapter_attached(room)
                except Exception:
                    pass
                gaze = {
                    "speech": {"target": room, "params": {}},
                    "thoughts": {"target": "irc", "params": {"room": f"#{agent_lower}-thoughts"}}
                }
                gaze_str = room
            else:
                gaze = {
                    "speech": {"target": "irc", "params": {"room": room}},
                    "thoughts": {"target": "irc", "params": {"room": f"#{agent_lower}-thoughts"}}
                }
                gaze_str = f"irc/{room}"

        try:
            with open(Config.gaze_file(), "w") as f:
                json.dump(gaze, f)
        except Exception as e:
            self.app.notify(f"Failed to write gaze: {e}", severity="error")
            return
        try:
            header = self.app.query_one("#agent-header", AgentHeader)
            header.gaze_target = gaze_str
        except NoMatches:
            pass

        self.app.notify(f"Gaze set to {gaze_str}", severity="information")
        self.display = False
        self.app.query_one("#input-bar", MessageInput).focus()


class SlashMenu(OptionList):
    """Autocomplete popup for slash commands. Appears above the input bar."""

    DEFAULT_CSS = """
    SlashMenu {
        layer: overlay;
        dock: bottom;
        margin: 0 0 6 1;
        width: 50;
        max-height: 14;
        border: solid $accent;
        background: $surface;
        display: none;
    }
    """

    # Built-in local commands
    LOCAL_COMMANDS = [
        {"name": "/clear", "description": "Clear the screen"},
        {"name": "/status", "description": "Show agent status"},
        {"name": "/gaze", "description": "Show/change gaze target"},
        {"name": "/awareness", "description": "Show/edit background channels"},
        {"name": "/awareness add", "description": "Add background channel"},
        {"name": "/awareness rm", "description": "Remove background channel"},
        {"name": "/health", "description": "Show health info"},
        {"name": "/todo", "description": "Manage persistent todo list"},
        {"name": "/todo add", "description": "Add a todo item"},
        {"name": "/todo done", "description": "Mark item as done"},
        {"name": "/todo rm", "description": "Remove a todo item"},
        {"name": "/mail", "description": "Send localmail to an agent"},
        {"name": "/mail all", "description": "Broadcast to all agents"},
        {"name": "/whoami", "description": "Show/change operator name"},
        {"name": "/help", "description": "Show help"},
        {"name": "/exit", "description": "Quit the TUI"},
    ]

    def populate(self, filter_text: str = "/", agent_commands: list = None):
        """Populate with matching commands."""
        self.clear_options()
        prefix = filter_text.lower()

        all_commands = list(self.LOCAL_COMMANDS)
        if agent_commands:
            for cmd in agent_commands:
                name = cmd.get("name", cmd.get("command", ""))
                desc = cmd.get("description", "")
                if name and not name.startswith("/"):
                    name = f"/{name}"
                if name and not any(c["name"] == name for c in all_commands):
                    all_commands.append({"name": name, "description": desc})

        matched = 0
        for cmd in all_commands:
            name = cmd["name"]
            desc = cmd.get("description", "")
            if name.lower().startswith(prefix) or prefix == "/":
                label = f"  {name:<16} {desc}"
                self.add_option(Option(label, id=name))
                matched += 1

        return matched > 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Insert selected command into input."""
        cmd_name = event.option.id
        if cmd_name is None:
            return
        try:
            input_bar = self.app.query_one("#input-bar", MessageInput)
            input_bar.clear()
            input_bar.insert(cmd_name + " ")
            input_bar.focus()
        except NoMatches:
            pass
        self.display = False


class SystemAlert(Static):
    """System notification bar for retry, doom loop, compaction events."""

    def __init__(self, message: str, severity: str = "warning", **kwargs):
        super().__init__(**kwargs)
        self.alert_message = message
        self.severity = severity

    def render(self) -> Text:
        text = Text()
        if self.severity == "error":
            text.append(" ⚠ ", style=f"bold {Gruvbox.BR_RED}")
            text.append(self.alert_message, style=Gruvbox.BR_RED)
        elif self.severity == "warning":
            text.append(" ⚠ ", style=f"bold {Gruvbox.BR_YELLOW}")
            text.append(self.alert_message, style=Gruvbox.BR_YELLOW)
        else:
            text.append(" ℹ ", style=f"bold {Gruvbox.BR_BLUE}")
            text.append(self.alert_message, style=Gruvbox.BR_BLUE)
        return text


class MessageInput(TextArea):
    """Multiline input with mode toggle. Normal: Enter sends, ^J newline. Edit: Enter newline, ^J sends."""

    multiline_mode = reactive(False)

    DEFAULT_CSS = """
    MessageInput {
        height: auto;
        min-height: 4;
        border: heavy #504945;
        padding: 0 1;
    }
    MessageInput:focus {
        border: heavy #7c6f64;
    }
    
    """

    class Submitted(TextArea.Changed):
        """Fired when user presses Enter (without Shift)."""
        def __init__(self, text_area: "MessageInput", text: str):
            super().__init__(text_area)
            self.text = text

    def __init__(self, placeholder: str = "", **kwargs):
        super().__init__("", language=None, show_line_numbers=False, **kwargs)
        self._placeholder = placeholder
        self._history: list[str] = []
        self._history_index: int = -1
        self._draft: str = ""  # Saves current input when browsing history
        # Register underscore cursor theme
        from textual.widgets.text_area import TextAreaTheme
        from rich.style import Style
        underscore_theme = TextAreaTheme(
            name="underscore",
            cursor_style=Style(underline=True),
            cursor_line_style=Style(),
        )
        self.register_theme(underscore_theme)
        self.theme = "underscore"
        self._update_mode_label()

    async def _on_paste(self, event) -> None:
        """Handle bracketed paste. Multi-line paste auto-switches to edit mode."""
        if self.read_only:
            return
        text = event.text
        if "\n" in text:
            self.multiline_mode = True
        self.insert(text)
        self.focus()
        event.prevent_default()
        event.stop()

    def _update_mode_label(self) -> None:
        """Update border title to show current input mode."""
        if self.multiline_mode:
            self.border_subtitle = "EDIT: Enter=newline ^J=send | ^E=normal"
        else:
            self.border_subtitle = ""
        self.border_title = ""

    def watch_multiline_mode(self, value: bool) -> None:
        """React to mode toggle — update border subtitle."""
        self._update_mode_label()

    def _get_wrap_width(self) -> int:
        """Get the actual character width available for text wrapping."""
        try:
            region = self.scrollable_content_region
            return max(region.width, 1)
        except Exception:
            return max(self.size.width - 4, 1)

    def _is_multiline(self) -> bool:
        """Check if input has multiple visual lines (newlines or wrapping)."""
        if "\n" in self.text:
            return True
        return len(self.text) > self._get_wrap_width()

    def undo(self) -> None:
        """Override undo to handle Textual cursor desync bug.

        Textual's _undo_batch calls _refresh_size() before updating the cursor.
        With auto-height, the scrollbar refresh tries to scroll to a cursor
        position that references lines removed by the undo.
        """
        try:
            super().undo()
        except ValueError:
            line_count = self.document.line_count
            row = min(self.cursor_location[0], max(0, line_count - 1))
            last_line = self.document.get_line(row)
            col = min(self.cursor_location[1], len(last_line))
            self.move_cursor((row, col))
            try:
                self._refresh_size()
            except ValueError:
                pass

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Recalculate height when text changes, using TextArea's own virtual size."""
        def _update_height():
            visual_lines = max(self.virtual_size.height, 1)
            target_height = max(2, min(visual_lines + 2, 10))  # +2 for borders
            self.styles.height = target_height
            if visual_lines > 8:
                self.scroll_cursor_visible()
        self.call_after_refresh(_update_height)

    def _on_key(self, event) -> None:
        """Handle input keys. Ctrl+E toggles mode. Mode determines Enter vs Ctrl+J behavior."""
        # Pass Home/End/PageUp/PageDown to the app for scroll/history actions
        if event.key in ("home", "end", "pageup", "pagedown"):
            event.prevent_default()
            event.stop()
            if event.key == "home":
                self.app.action_scroll_top()
            elif event.key == "end":
                self.app.action_scroll_bottom()
            elif event.key == "pageup":
                self.app.action_load_history()
            elif event.key == "pagedown":
                try:
                    scroll = self.app._content_scroll()
                    scroll.scroll_page_down(animate=False)
                except Exception:
                    pass
            return
        # Ctrl+E toggles multiline mode
        if event.key == "ctrl+e":
            event.prevent_default()
            event.stop()
            self.multiline_mode = not self.multiline_mode
            return
        # Determine which key sends and which inserts newline based on mode
        if self.multiline_mode:
            send_key = "ctrl+j"
            newline_keys = ("enter", "shift+enter", "ctrl+enter")
        else:
            send_key = "enter"
            newline_keys = ("shift+enter", "ctrl+enter", "ctrl+j")
        if event.key in newline_keys:
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        elif event.key == send_key:
            # If slash menu is visible, select the highlighted option
            try:
                slash_menu = self.app.query_one("#slash-menu")
                if slash_menu.display and slash_menu.highlighted is not None:
                    event.prevent_default()
                    event.stop()
                    slash_menu.action_select()
                    return
            except Exception:
                pass
            event.prevent_default()
            event.stop()
            text = self.text.strip()
            if text:
                self._history.append(text)
                self._history_index = -1
                self._draft = ""
                self.post_message(self.Submitted(self, text))
                self.clear()
        elif event.key in ("up", "down") and self._is_multiline():
            # Multiline: move cursor within text, prevent bubbling to parent scroll
            event.prevent_default()
            event.stop()
            if event.key == "up":
                self.action_cursor_up()
            else:
                self.action_cursor_down()
            return
        elif event.key == "up":
            # If slash menu is visible, navigate it
            try:
                slash_menu = self.app.query_one("#slash-menu")
                if slash_menu.display:
                    event.prevent_default()
                    event.stop()
                    slash_menu.action_cursor_up()
                    return
            except Exception:
                pass
            # Only use history nav when input is single-line
            event.prevent_default()
            event.stop()
            if self._history:
                if self._history_index == -1:
                    self._draft = self.text
                    self._history_index = len(self._history) - 1
                elif self._history_index > 0:
                    self._history_index -= 1
                self.clear()
                self.insert(self._history[self._history_index])
        elif event.key == "down":
            # If slash menu is visible, navigate it
            try:
                slash_menu = self.app.query_one("#slash-menu")
                if slash_menu.display:
                    event.prevent_default()
                    event.stop()
                    slash_menu.action_cursor_down()
                    return
            except Exception:
                pass
            event.prevent_default()
            event.stop()
            if self._history_index >= 0:
                if self._history_index < len(self._history) - 1:
                    self._history_index += 1
                    self.clear()
                    self.insert(self._history[self._history_index])
                else:
                    self._history_index = -1
                    self.clear()
                    self.insert(self._draft)


class ToolCallPanel(Static):
    """Renders a tool call as a bordered panel. Completed panels collapse to one line.
    Click to expand/collapse."""

    def __init__(self, tool_id: str, title: str, kind: str = "", **kwargs):
        super().__init__(**kwargs)
        self.tool_id = tool_id
        self.tool_title = title
        self.tool_kind = kind
        self.tool_status = "running"
        self.tool_output = ""
        self.border_title = title.replace("[", "\\[")
        self._collapsed = False

    def set_status(self, status: str):
        self.tool_status = status
        # Auto-collapse completed/failed panels
        if status in ("completed", "failed"):
            self._collapsed = True
        self.refresh(layout=True)

    def set_output(self, content: str):
        self.tool_output = content
        self.refresh(layout=True)

    def append_output(self, content: str):
        self.tool_output += content
        self.refresh(layout=True)

    MAX_ACTIVE_LINES = 15  # Max lines shown for active/running tool panels

    @staticmethod
    def _highlight_interjections(content: "Text", raw_output: str) -> "Text":
        """Re-render output with <interjection> blocks highlighted."""
        import re
        if "<interjection>" not in raw_output:
            return content
        # Rebuild text with interjection blocks styled distinctly
        result = Text()
        pos = 0
        for m in re.finditer(r"<interjection>\n?(.*?)</interjection>", raw_output, re.DOTALL):
            # Text before the interjection block
            if m.start() > pos:
                result.append(raw_output[pos:m.start()], style=Gruvbox.GRAY)
            # Interjection block with highlight
            result.append("🔔 ", style=Gruvbox.BR_ORANGE)
            result.append(m.group(1).strip(), style=f"bold {Gruvbox.BR_ORANGE}")
            result.append("\n", style=Gruvbox.GRAY)
            pos = m.end()
            if pos < len(raw_output) and raw_output[pos] == "\n":
                pos += 1
        # Text after last interjection
        if pos < len(raw_output):
            result.append(raw_output[pos:], style=Gruvbox.GRAY)
        return result

    def on_click(self, event) -> None:
        """Toggle collapsed state on click."""
        self._collapsed = not self._collapsed
        self.refresh(layout=True)

    def render(self):
        # Status indicator
        if self.tool_status == "completed":
            status_icon = "✓"
            border_style = Gruvbox.BR_GREEN
        elif self.tool_status == "failed":
            status_icon = "✗"
            border_style = Gruvbox.BR_RED
        elif self.tool_status == "in_progress":
            status_icon = "⟳"
            border_style = Gruvbox.BR_YELLOW
        else:
            status_icon = "…"
            border_style = Gruvbox.BR_BLUE

        # Kind icon
        kind_icons = {
            "read": "📖", "execute": "⚡", "edit": "✏️",
            "search": "🔍", "think": "💭", "other": "📋",
        }
        kind_icon = kind_icons.get(self.tool_kind, "🔧")

        title = f"{kind_icon} {self.tool_title} {status_icon}"

        # Collapsed: single line with hint
        if self._collapsed:
            text = Text()
            text.append(f"  {title}", style=border_style)
            if self.tool_output:
                text.append("  ▸ click to expand", style=Gruvbox.DARK4)
            return text

        # Expanded: full panel
        if self.tool_output:
            output = self.tool_output
            lines = output.split("\n")
            is_active = self.tool_status not in ("completed", "failed")

            if is_active and len(lines) > self.MAX_ACTIVE_LINES:
                # Active panel: show last MAX lines with "click for more"
                display = "\n".join(lines[-self.MAX_ACTIVE_LINES:])
                header = f"... ({len(lines) - self.MAX_ACTIVE_LINES} more lines above, click to expand) ...\n"
                content = Text(header, style=Gruvbox.DARK4)
                content.append(display, style=Gruvbox.GRAY)
            elif not is_active and len(output) > 2000:
                # Completed: truncate middle
                display = output[:1000] + "\n... (truncated) ...\n" + output[-500:]
                content = Text(display, style=Gruvbox.GRAY)
            else:
                content = Text(output, style=Gruvbox.GRAY)

            # Highlight <interjection> blocks with distinct styling
            content = self._highlight_interjections(content, output)
        else:
            content = Text("(no output)", style=f"italic {Gruvbox.DARK4}")

        return Panel(
            content,
            title=title,
            title_align="left",
            border_style=border_style,
            padding=(0, 1),
        )


class PlanPanel(Static):
    """Renders the agent's todo/plan list."""

    def __init__(self, entries: list, **kwargs):
        super().__init__(**kwargs)
        self.entries = entries

    def render(self) -> Panel:
        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("status", width=3)
        table.add_column("task")

        status_icons = {
            "completed": f"[{Gruvbox.BR_GREEN}]✓[/]",
            "in_progress": f"[{Gruvbox.BR_YELLOW}]▶[/]",
            "pending": f"[{Gruvbox.GRAY}]○[/]",
            "cancelled": f"[{Gruvbox.GRAY}]✗[/]",
        }

        for entry in self.entries:
            icon = status_icons.get(entry.get("status", "pending"), "?")
            content = entry.get("content", "")
            style = "dim" if entry.get("status") == "completed" else ""
            table.add_row(icon, Text(content, style=style))

        return Panel(table, title="📋 Plan", title_align="left",
                     border_style=Gruvbox.BR_PURPLE, padding=(0, 1))


class RoomMessage(Static):
    """A single IRC channel message displayed in the room tab."""

    DEFAULT_CSS = """
    RoomMessage {
        padding: 0 1;
        margin: 0;
    }
    """

    NICK_COLORS = [
        Gruvbox.BR_YELLOW, Gruvbox.BR_GREEN, Gruvbox.BR_BLUE,
        Gruvbox.BR_PURPLE, Gruvbox.BR_AQUA, Gruvbox.BR_ORANGE, Gruvbox.BR_RED,
    ]

    def __init__(self, timestamp: str, nick: str, message: str, is_action: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._timestamp = timestamp
        self._nick = nick
        self._message = message
        self._is_action = is_action

    def render(self) -> Text:
        color = self.NICK_COLORS[hash(self._nick.lower()) % len(self.NICK_COLORS)]
        ts_style = Gruvbox.DARK4

        text = Text()
        text.append(f"{self._timestamp} ", style=ts_style)
        if self._is_action:
            text.append(f"* {self._nick} ", style=f"italic {color}")
            text.append(self._message, style=f"italic {Gruvbox.FG}")
        else:
            text.append(f"<{self._nick}> ", style=f"bold {color}")
            text.append(self._message, style=Gruvbox.FG)
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
        text.append(f"{self._timestamp} ", style=Gruvbox.DARK4)
        text.append(self._message, style=f"italic {Gruvbox.DARK3}")
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

    active_agent = reactive("Trip")

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
                text.append(f" [{label}] ", style=f"bold {Gruvbox.FG} on {Gruvbox.DARK2}")
            else:
                text.append(f"  {label}  ", style=Gruvbox.DARK4)
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
            text.append(f" {key} ", style=f"bold {Gruvbox.BR_ORANGE}")
            text.append(f"{label} ", style=Gruvbox.FG)
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
        return Text(f"  {self.annotation_message}", style=f"italic {Gruvbox.DARK4}")


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
        text.append(" T", style=f"bold {Gruvbox.DARK4}")
        text.append(f"{self._turn_num}", style=f"bold {Gruvbox.BR_AQUA}")
        text.append(f" {self._trigger}", style=Gruvbox.DARK4)
        if self._timestamp:
            text.append(f"  {self._timestamp}", style=Gruvbox.DARK3)
        # Fill remaining width with thin line
        pad = max(0, 60 - len(text.plain))
        text.append(" " + "\u2500" * pad, style=Gruvbox.DARK3)
        return text


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


class UserMessage(Static):
    """User message display -- clean inline style with chevron prefix."""

    def __init__(self, text: str, **kwargs):
        super().__init__(**kwargs)
        self.user_text = text

    def render(self) -> Text:
        text = Text()
        text.append("❯ ", style=f"bold {Gruvbox.BR_BLUE}")
        text.append(self.user_text, style=Gruvbox.FG)
        return text


class AgentMessage(Static):
    """Agent message display — renders accumulated markdown."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._chunks: list[str] = []
        self._text = ""

    def append_chunk(self, text: str):
        self._chunks.append(text)
        self._text = "".join(self._chunks)
        self.refresh(layout=True)

    @property
    def full_text(self) -> str:
        return self._text

    @staticmethod
    def _format_interjections(text: str) -> str:
        """Replace <interjection> blocks with styled markdown blockquotes."""
        import re
        if "<interjection>" not in text:
            return text
        def _repl(m):
            body = m.group(1).strip()
            lines = body.split("\n")
            quoted = "\n".join(f"> {line}" for line in lines)
            return f"\n> 🔔 **[interjection]**\n{quoted}\n"
        return re.sub(r"<interjection>\n?(.*?)</interjection>", _repl, text, flags=re.DOTALL)

    def render(self) -> RichMarkdown:
        return RichMarkdown(self._format_interjections(self._text))


class ThinkingBlock(Static):
    """Dimmed thinking/reasoning block with token counter. Click to expand/collapse."""

    TRUNCATE_THRESHOLD = 50  # Lines before truncation kicks in
    HEAD_LINES = 15          # Lines shown at top when truncated
    TAIL_LINES = 15          # Lines shown at bottom when truncated

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._chunks: list[str] = []
        self._text = ""
        self._token_estimate = 0
        self._expanded = False

    def append_chunk(self, text: str):
        self._chunks.append(text)
        self._text = "".join(self._chunks)
        self._token_estimate = len(self._text) // 4
        self.refresh(layout=True)

    def on_click(self, event) -> None:
        """Toggle expanded/collapsed state."""
        self._expanded = not self._expanded
        self.refresh(layout=True)

    def render(self) -> Panel:
        text = self._text
        lines = text.split("\n")
        total = len(lines)
        hidden = total - self.HEAD_LINES - self.TAIL_LINES

        if not self._expanded and total > self.TRUNCATE_THRESHOLD:
            display = (
                "\n".join(lines[:self.HEAD_LINES])
                + f"\n... ({hidden} more lines — click to expand) ...\n"
                + "\n".join(lines[-self.TAIL_LINES:])
            )
        else:
            display = text

        # Token count in title
        if self._token_estimate > 0:
            title_str = f"💭 Thinking (↓ ~{self._token_estimate} tokens)"
        else:
            title_str = "💭 Thinking"

        if self._expanded and total > self.TRUNCATE_THRESHOLD:
            title_str += " [expanded — click to collapse]"

        return Panel(
            Text(display, style=Gruvbox.DARK4),
            title=title_str,
            title_align="left",
            border_style=Gruvbox.DARK3,
            padding=(0, 1),
        )


# =============================================================================
# Operator Identity Screen
# =============================================================================

class OperatorScreen(ModalScreen[str]):
    """Ask the operator for their name on first launch."""

    DEFAULT_CSS = """
    OperatorScreen {
        align: center middle;
    }
    OperatorScreen > Vertical {
        width: 50;
        height: auto;
        max-height: 12;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    OperatorScreen Static {
        width: 100%;
        content-align: center middle;
        margin-bottom: 1;
    }
    OperatorScreen Input {
        width: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Who are you?")
            yield Input(placeholder="Your name...", id="operator-input")

    def on_mount(self) -> None:
        self.query_one("#operator-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        if name:
            self.dismiss(name)


# =============================================================================
# Persistence Panel
# =============================================================================

class PersistenceScreen(ModalScreen[None]):
    """Shows persistence management state for the active agent."""

    DEFAULT_CSS = """
    PersistenceScreen {
        align: center middle;
    }
    PersistenceScreen > Vertical {
        width: 72;
        height: auto;
        max-height: 30;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
        overflow-y: auto;
    }
    PersistenceScreen .section-title {
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_panel", "Close"),
        Binding("f2", "dismiss_panel", "Close"),
    ]

    def __init__(self, agent_name: str, agent_dir: Path, **kwargs):
        super().__init__(**kwargs)
        self._agent_name = agent_name
        self._agent_dir = agent_dir

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._build_content())

    def _build_content(self) -> Text:
        text = Text()
        agent = self._agent_name
        d = self._agent_dir

        text.append(f" Persistence: {agent} ", style=f"bold {Gruvbox.FG} on {Gruvbox.DARK2}")
        text.append("\n")

        # --- Health ---
        text.append("\n Health ", style=f"bold {Gruvbox.BR_AQUA}")
        text.append("\n")
        health_path = d / "asdaaas" / "health.json"
        try:
            with open(health_path) as f:
                h = json.load(f)
            tokens = h.get("totalTokens", "?")
            ctx_win = h.get("contextWindow", "?")
            pct = int(tokens / ctx_win * 100) if isinstance(tokens, int) and isinstance(ctx_win, int) else "?"
            text.append(f"  Context: {tokens}/{ctx_win} ({pct}%)\n", style=Gruvbox.FG)
            text.append(f"  Status: {h.get('status', '?')}  Detail: {h.get('detail', '')}\n", style=Gruvbox.FG)
            text.append(f"  Model: {h.get('model', '?')}\n", style=Gruvbox.FG)
            text.append(f"  Last activity: {h.get('last_activity', '?')}\n", style=Gruvbox.FG)
        except Exception as e:
            text.append(f"  (could not read: {e})\n", style=Gruvbox.BR_RED)

        # --- Lab Notebook ---
        text.append("\n Lab Notebook ", style=f"bold {Gruvbox.BR_GREEN}")
        text.append("\n")
        notebook = d / f"lab_notebook_{agent.lower()}.md"
        if notebook.exists():
            import os as _os
            stat = _os.stat(notebook)
            size_kb = stat.st_size / 1024
            mtime = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            text.append(f"  File: {notebook.name}\n", style=Gruvbox.FG)
            text.append(f"  Size: {size_kb:.0f} KB  Modified: {mtime}\n", style=Gruvbox.FG)
            # Find last ### entry header
            try:
                with open(notebook, "rb") as f:
                    f.seek(max(0, stat.st_size - 2000))
                    tail = f.read().decode("utf-8", errors="replace")
                entries = [l for l in tail.split("\n") if l.startswith("### ")]
                if entries:
                    text.append(f"  Last entry: {entries[-1][:60]}\n", style=Gruvbox.FG)
            except Exception:
                pass
        else:
            text.append(f"  (not found: {notebook})\n", style=Gruvbox.BR_RED)

        # --- Notes to Self ---
        text.append("\n Notes to Self ", style=f"bold {Gruvbox.BR_YELLOW}")
        text.append("\n")
        # Try common naming patterns
        notes = None
        for pattern in [f"MikeyV_{agent}_notes_to_self.md", f"notes_to_self.md", f"{agent}_notes.md"]:
            candidate = d / pattern
            if candidate.exists():
                notes = candidate
                break
        if notes:
            import os as _os
            stat = _os.stat(notes)
            size_kb = stat.st_size / 1024
            mtime = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            text.append(f"  File: {notes.name}\n", style=Gruvbox.FG)
            text.append(f"  Size: {size_kb:.0f} KB  Modified: {mtime}\n", style=Gruvbox.FG)
        else:
            text.append("  (not found)\n", style=Gruvbox.DARK4)

        # --- Git ---
        text.append("\n Git ", style=f"bold {Gruvbox.BR_PURPLE}")
        text.append("\n")
        try:
            import subprocess
            result = subprocess.run(
                ["git", "log", "--oneline", "-3", "--format=%h %ar  %s", "--", str(d)],
                cwd=str(d.parent), capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().split("\n")[:3]:
                    text.append(f"  {line}\n", style=Gruvbox.FG)
            else:
                text.append("  (no recent commits)\n", style=Gruvbox.DARK4)
        except Exception as e:
            text.append(f"  (git error: {e})\n", style=Gruvbox.BR_RED)

        # --- Compaction History ---
        text.append("\n Compaction ", style=f"bold {Gruvbox.BR_ORANGE}")
        text.append("\n")
        try:
            with open(health_path) as f:
                h = json.load(f)
            detail = h.get("detail", "")
            if "compacted" in detail:
                text.append(f"  {detail}\n", style=Gruvbox.FG)
            else:
                text.append(f"  Detail: {detail}\n", style=Gruvbox.FG)
        except Exception:
            text.append("  (unknown)\n", style=Gruvbox.DARK4)

        # --- Session Profile ---
        text.append("\n Session ", style=f"bold {Gruvbox.BR_BLUE}")
        text.append("\n")
        profile_dir = d / "asdaaas" / "profile"
        if profile_dir.exists():
            latest = profile_dir / f"{agent}_latest.json"
            jsonl = profile_dir / f"{agent}.jsonl"
            if jsonl.exists():
                try:
                    with open(jsonl, "rb") as f:
                        turn_count = sum(1 for _ in f)
                    text.append(f"  Physical turns: {turn_count}\n", style=Gruvbox.FG)
                except Exception:
                    pass
            if latest.exists():
                try:
                    with open(latest) as f:
                        lp = json.load(f)
                    text.append(f"  Last turn: {lp.get('ts', '?')}\n", style=Gruvbox.FG)
                    text.append(f"  Duration: {lp.get('wall_seconds', '?')}s\n", style=Gruvbox.FG)
                except Exception:
                    pass

        # --- Doorbells ---
        bells_dir = d / "asdaaas" / "doorbells"
        if bells_dir.exists():
            bell_files = list(bells_dir.glob("*.json"))
            if bell_files:
                text.append(f"\n  Pending doorbells: {len(bell_files)}\n", style=Gruvbox.BR_YELLOW)

        text.append("\n")
        text.append(" Press Esc or F2 to close ", style=Gruvbox.DARK4)
        return text

    def action_dismiss_panel(self) -> None:
        self.dismiss(None)


# =============================================================================
# Main Application
# =============================================================================

class AsdaaasTUI(App):
    """Full-screen TUI for asdaaas agent sessions."""

    TITLE = "asdaaas TUI"
    SUB_TITLE = "Development Interface"

    CSS = """
    Screen {
        layout: vertical;
        layers: default overlay;
    }

    #top-bar {
        dock: top;
        height: auto;
        max-height: 2;
    }

    #agent-tab-bar {
        height: 1;
        background: #3c3836;
    }

    #agent-header {
        height: 1;
        background: $surface;
    }

    VerticalScroll {
        height: 1fr;
        scrollbar-size: 1 1;
        padding: 0 1;
    }

    #input-bar {
        margin: 0 0;
    }

    #bottom-bar {
        dock: bottom;
        height: auto;
        max-height: 12;
    }

    AgentMessage {
        margin: 0 0 1 0;
    }

    ToolCallPanel {
        margin: 0 0 0 2;
    }

    PlanPanel {
        margin: 0 0 1 0;
    }

    HookAnnotation {
        margin: 0 0 0 0;
        height: auto;
    }

    UserMessage {
        margin: 0 0 1 1;
    }

    TurnSeparator {
        margin: 1 0 0 0;
        height: 1;
    }

    ThinkingBlock {
        margin: 0 0 0 2;
    }

    SystemAlert {
        margin: 0 0 0 0;
        height: auto;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "interrupt_agent", "Interrupt", show=True),
        Binding("ctrl+q", "quit", "Quit", show=False),
        Binding("ctrl+l", "clear_screen", "Clear", show=True),
        Binding("ctrl+g", "toggle_gaze_selector", "Gaze", show=True),
        Binding("escape", "dismiss_overlay", "Dismiss", show=False),
        Binding("f1", "toggle_thinking", "Toggle Thinking", show=True),
        Binding("end", "scroll_bottom", "Bottom", show=False),
        Binding("home", "scroll_top", "Top", show=False, priority=True),
        Binding("pageup", "load_history", "Load History", show=False, priority=True),
        Binding("ctrl+n", "next_agent", "Next Agent", show=False),
        Binding("f2", "show_persistence", "Persistence", show=True),
    ]

    def __init__(self, agents: list[str] = None, **kwargs):
        super().__init__(**kwargs)
        self._agents = agents or [Config.AGENT_NAME]
        self._active_agent = Config.AGENT_NAME
        # Per-agent state
        self._agent_state: dict[str, dict] = {}
        for agent in self._agents:
            self._agent_state[agent] = {
                "tool_panels": {},
                "current_agent_msg": None,
                "current_thinking": None,
                "updates_offset": 0,
                "replay_done": False,
                "earliest_offset": 0,  # File offset of earliest loaded event
                "updates_path": None,  # Cached path to updates.jsonl
                "loading_history": False,  # Prevents concurrent loads
                "input_draft": "",  # Saved input text when switching tabs
                "backend": Config.agent_backend(agent),  # "grok" or "claude"
                "logical_turn": 0,  # Logical turn counter (user_message_chunk events)
            }
        # Shared state
        self._abide_head = self._get_abide_head()
        self._replay_mode: bool = False
        self._replay_done: bool = False
        self._tail_count: Optional[int] = None
        self._show_thinking: bool = True
        self._available_commands: list[dict] = []

    @staticmethod
    def _get_abide_head() -> str:
        """Get current git HEAD of agent-abide repo. Returns short hash or ''."""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(Path.home() / "projects" / "agent-abide"),
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            return ""

    @property
    def _tool_panels(self) -> dict:
        return self._agent_state[self._active_agent]["tool_panels"]

    @property
    def _current_agent_msg(self) -> Optional[AgentMessage]:
        return self._agent_state[self._active_agent]["current_agent_msg"]

    @_current_agent_msg.setter
    def _current_agent_msg(self, val):
        self._agent_state[self._active_agent]["current_agent_msg"] = val

    @property
    def _current_thinking(self) -> Optional[ThinkingBlock]:
        return self._agent_state[self._active_agent]["current_thinking"]

    @_current_thinking.setter
    def _current_thinking(self, val):
        self._agent_state[self._active_agent]["current_thinking"] = val

    @property
    def _updates_offset(self) -> int:
        return self._agent_state[self._active_agent]["updates_offset"]

    @_updates_offset.setter
    def _updates_offset(self, val):
        self._agent_state[self._active_agent]["updates_offset"] = val

    def _content_scroll(self, agent: str = None) -> ContentScroll:
        """Get the content scroll widget for the given agent (or active agent)."""
        agent = agent or self._active_agent
        return self.query_one(f"#content-{agent}", ContentScroll)

    def compose(self) -> ComposeResult:
        with Vertical(id="top-bar"):
            if len(self._agents) > 1:
                yield AgentTabBar(self._agents, id="agent-tab-bar")
            yield AgentHeader(id="agent-header")
        yield GazeSelector(id="gaze-selector")
        yield SlashMenu(id="slash-menu")
        # One content scroll per agent
        for agent in self._agents:
            vs = ContentScroll(id=f"content-{agent}")
            if agent != self._active_agent:
                vs.display = False
            yield vs
        # Room content scroll (IRC channel view)
        room_vs = ContentScroll(id="content-room")
        room_vs.display = False
        yield room_vs
        with Vertical(id="bottom-bar"):
            yield MessageInput(placeholder=f"Message {Config.AGENT_NAME}...", id="input-bar")
            yield DynamicFooter(id="dynamic-footer")

    def _cleanup_and_exit(self) -> None:
        """Clean up background threads before exiting.

        Closes IRC socket (unblocks keepalive thread), then calls exit().
        Workers check is_cancelled but some block on I/O or sleep —
        closing sockets ensures they unblock and see the cancellation.
        """
        # Close IRC socket so keepalive + send threads unblock
        if hasattr(self, "_room_irc_sock") and self._room_irc_sock is not None:
            try:
                self._room_irc_sock.close()
            except Exception:
                pass
            self._room_irc_sock = None
        self.exit()

    def on_mount(self) -> None:
        """Start background workers on mount."""
        # Check operator identity
        if not Config.OPERATOR_NAME:
            saved = Config.load_operator()
            if saved:
                Config.OPERATOR_NAME = saved
            else:
                self.push_screen(OperatorScreen(), self._on_operator_set)
                return  # Workers start after operator is set

        self._start_workers()

    def _on_operator_set(self, name: str) -> None:
        """Callback when operator enters their name."""
        Config.OPERATOR_NAME = name
        Config.save_operator(name)
        self._start_workers()

    def _start_workers(self) -> None:
        """Start background workers and initialize UI."""
        # Start the status poller
        self.status_worker = self.run_worker(
            self._poll_status, thread=True, name="status_poller"
        )
        # Start updates tailer for each agent
        # TODO: conversation.jsonl disabled pending schema redesign (Phase 2)
        use_api = Config.API_URL is not None
        for agent in self._agents:
            state = self._agent_state.get(agent, {})
            if use_api:
                self.run_worker(
                    lambda a=agent: self._tail_via_api(a),
                    thread=True, name=f"updates_{agent}"
                )
            else:
                self.run_worker(
                    lambda a=agent: self._tail_updates_for_agent(a),
                    thread=True, name=f"updates_{agent}"
                )
        # Start IRC room log tailer
        self._room_channel = "#meetingroom1"
        self._room_active = False
        self._room_irc_sock = None
        self.run_worker(self._tail_room_log, thread=True, name="room_tailer")
        # Focus the input bar
        self.query_one("#input-bar", MessageInput).focus()

        # Set the header
        header = self.query_one("#agent-header", AgentHeader)
        header.agent_name = Config.AGENT_NAME

    # -------------------------------------------------------------------------
    # Input handling
    # -------------------------------------------------------------------------

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Show/hide slash menu as user types."""
        if not isinstance(event.text_area, MessageInput):
            return
        text = event.text_area.text
        try:
            slash_menu = self.query_one("#slash-menu", SlashMenu)
            if text.startswith("/") and "\n" not in text:
                has_matches = slash_menu.populate(
                    text, self._available_commands
                )
                slash_menu.display = has_matches
            else:
                slash_menu.display = False
        except NoMatches:
            pass

    def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        """Handle user input submission."""
        # Dismiss slash menu on submit
        try:
            self.query_one("#slash-menu", SlashMenu).display = False
        except NoMatches:
            pass

        text = event.text.strip()
        if not text:
            return

        # Handle slash commands locally
        if text.startswith("/"):
            self._handle_slash_command(text)
            return

        # Room mode: send to IRC channel instead of agent
        if self._room_active:
            nick = Config.OPERATOR_NAME or "eric"
            try:
                room_scroll = self.query_one("#content-room", ContentScroll)
                room_scroll._follow_tail = True
                ts = time.strftime("%H:%M")
                room_scroll.mount(RoomMessage(ts, nick, text))
                room_scroll.scroll_end(animate=False)
            except NoMatches:
                pass
            import threading
            threading.Thread(target=self._send_to_room, args=(text,), daemon=True).start()
            return

        # Display the user message — re-enable tail following
        content = self._content_scroll()
        content._follow_tail = True
        content.mount(UserMessage(text))
        self._scroll_to_bottom()

        # Reset message state for new response
        self._current_agent_msg = None
        self._current_thinking = None
        # Mark as generating (agent will respond)
        try:
            header = self.query_one("#agent-header", AgentHeader)
            header.is_generating = True
        except NoMatches:
            pass
        try:
            footer = self.query_one("#dynamic-footer", DynamicFooter)
            footer.is_generating = True
        except NoMatches:
            pass

        # Track for double-display prevention
        self._last_sent_text = text

        # Write to asdaaas TUI adapter inbox
        self._send_to_adapter(text)

    def _send_to_adapter(self, text: str):
        """Write user message to the active agent's TUI adapter inbox."""
        agent_dir = Path(Config.AGENTS_HOME) / self._active_agent
        inbox = agent_dir / "asdaaas" / "adapters" / "tui" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        rand = secrets.token_hex(4)
        operator = Config.OPERATOR_NAME or "tui"
        msg = {
            "from": operator,
            "adapter": "tui",
            "text": text,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "meta": {"room": "tui", "operator": operator},
        }
        msg_path = inbox / f"msg_{ts}_{rand}.json"
        with open(msg_path, "w") as f:
            json.dump(msg, f)

    def _handle_slash_command(self, text: str):
        """Handle local slash commands."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        content = self._content_scroll()

        if cmd in ("/exit", "/quit", "/q"):
            self._cleanup_and_exit()
            return
        elif cmd == "/clear":
            content.remove_children()
            return
        elif cmd == "/status":
            self._show_status_info(content)
            return
        elif cmd == "/gaze":
            if arg:
                # Set gaze to the specified room
                self._set_gaze_to_room(arg.strip())
                return
            self._show_gaze_info(content)
            return
        elif cmd == "/health":
            self._show_health_info(content)
            return
        elif cmd == "/todo":
            self._handle_todo_command(arg, content)
            return
        elif cmd == "/whoami":
            if arg:
                Config.OPERATOR_NAME = arg.strip()
                Config.save_operator(arg.strip())
                msg = AgentMessage()
                content.mount(msg)
                msg.append_chunk(f"Operator name set to: **{arg.strip()}**")
                self._scroll_to_bottom()
            else:
                name = Config.OPERATOR_NAME or "unknown"
                msg = AgentMessage()
                content.mount(msg)
                msg.append_chunk(f"You are: **{name}**\n\nUse `/whoami <name>` to change.")
                self._scroll_to_bottom()
            return
        elif cmd == "/mail":
            self._handle_mail_command(arg, content)
            return
        elif cmd == "/awareness":
            if not arg:
                # Show current awareness
                self._show_awareness_info(content)
            elif arg.startswith("add "):
                parts = arg[4:].strip().split(None, 1)
                channel = parts[0] if parts else ""
                mode = parts[1] if len(parts) > 1 else "doorbell"
                if channel:
                    Config.write_command({"action": "awareness", "add": channel, "mode": mode})
                    m = AgentMessage(); content.mount(m)
                    m.append_chunk(f"Added **{channel}** as **{mode}**")
                    self._scroll_to_bottom()
            elif arg.startswith("rm ") or arg.startswith("remove "):
                channel = arg.split(None, 1)[1].strip() if " " in arg else ""
                if channel:
                    Config.write_command({"action": "awareness", "remove": channel})
                    m = AgentMessage(); content.mount(m)
                    m.append_chunk(f"Removed **{channel}**")
                    self._scroll_to_bottom()
            else:
                m = AgentMessage(); content.mount(m)
                m.append_chunk("Usage: `/awareness` | `/awareness add <channel> [doorbell|pending|drop]` | `/awareness rm <channel>`")
                self._scroll_to_bottom()
            return
        elif cmd == "/help":
            help_text = """## TUI Commands

| Command | Description |
|---------|-------------|
| `/clear` | Clear the screen |
| `/status` | Show agent status |
| `/gaze [room]` | Show/set gaze target |
| `/awareness` | Show/edit background channels |
| `/health` | Show health info |
| `/todo` | Manage persistent todo list |
| `/todo add <text>` | Add a todo item |
| `/todo done <n>` | Mark item n as done |
| `/todo rm <n>` | Remove item n |
| `/mail <agent> <msg>` | Send localmail to an agent |
| `/mail all <msg>` | Broadcast to all agents |
| `/whoami` | Show/change operator name |
| `/help` | Show this help |
| `Ctrl+C` | Interrupt agent |
| `Ctrl+Q` | Quit TUI |
| `Ctrl+L` | Clear screen |
| `Ctrl+G` | Gaze selector |
| `Ctrl+N` | Next agent tab |
| `F1` | Toggle thinking blocks |

Type anything else to send a message to the agent.
"""
            msg = AgentMessage()
            content.mount(msg)
            msg.append_chunk(help_text)
            self._scroll_to_bottom()
            return
        else:
            # Unknown command — send to agent anyway
            self._send_to_adapter(text)

    def _todo_file(self) -> Path:
        """Path to the persistent todo file for the active agent."""
        return Path(Config.AGENTS_HOME) / self._active_agent / "tui_todos.json"

    def _load_todos(self) -> list[dict]:
        """Load todos from disk."""
        path = self._todo_file()
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save_todos(self, todos: list[dict]) -> None:
        """Save todos to disk."""
        path = self._todo_file()
        with open(path, "w") as f:
            json.dump(todos, f, indent=2)

    def _handle_todo_command(self, arg: str, content: VerticalScroll) -> None:
        """Handle /todo subcommands."""
        parts = arg.strip().split(maxsplit=1)
        subcmd = parts[0].lower() if parts else "list"
        subarg = parts[1] if len(parts) > 1 else ""

        todos = self._load_todos()

        if subcmd == "add" and subarg:
            todos.append({"text": subarg, "done": False})
            self._save_todos(todos)
            self.notify(f"Added: {subarg}", severity="information")
        elif subcmd in ("done", "check") and subarg:
            try:
                idx = int(subarg) - 1
                if 0 <= idx < len(todos):
                    todos[idx]["done"] = True
                    self._save_todos(todos)
                    self.notify(f"Done: {todos[idx]['text']}", severity="information")
                else:
                    self.notify(f"Invalid index: {subarg}", severity="error")
            except ValueError:
                self.notify("Usage: /todo done <number>", severity="error")
        elif subcmd in ("rm", "remove", "del") and subarg:
            try:
                idx = int(subarg) - 1
                if 0 <= idx < len(todos):
                    removed = todos.pop(idx)
                    self._save_todos(todos)
                    self.notify(f"Removed: {removed['text']}", severity="information")
                else:
                    self.notify(f"Invalid index: {subarg}", severity="error")
            except ValueError:
                self.notify("Usage: /todo rm <number>", severity="error")
        elif subcmd in ("list", ""):
            pass  # Fall through to display
        else:
            self.notify("Usage: /todo [add|done|rm|list] [args]", severity="warning")
            return

        # Display current todos
        if not todos:
            display = "## Todo List\n\n*No items. Use `/todo add <text>` to add one.*"
        else:
            lines = ["## Todo List\n"]
            for i, item in enumerate(todos, 1):
                check = "✓" if item["done"] else "○"
                style = "~~" if item["done"] else ""
                text = item["text"]
                if style:
                    lines.append(f"  {i}. {check} {style}{text}{style}")
                else:
                    lines.append(f"  {i}. {check} {text}")
            lines.append(f"\n*{sum(1 for t in todos if t['done'])}/{len(todos)} done*")
            display = "\n".join(lines)

        msg = AgentMessage()
        content.mount(msg)
        msg.append_chunk(display)
        self._scroll_to_bottom()

    def _handle_mail_command(self, arg: str, content: VerticalScroll) -> None:
        """Handle /mail <agent> <message> and /mail all <message>."""
        KNOWN_AGENTS = ["Sr", "Jr", "Trip", "Q", "Cinco", "Astro"]

        if not arg.strip():
            m = AgentMessage(); content.mount(m)
            m.append_chunk(
                "## Localmail\n\n"
                "Usage:\n"
                "- `/mail <agent> <message>` -- send to one agent\n"
                "- `/mail all <message>` -- broadcast to all agents\n\n"
                f"Known agents: {', '.join(KNOWN_AGENTS)}"
            )
            self._scroll_to_bottom()
            return

        parts = arg.strip().split(maxsplit=1)
        target = parts[0]
        message = parts[1] if len(parts) > 1 else ""

        if not message:
            self.notify("Usage: /mail <agent> <message>", severity="error")
            return

        from_name = Config.OPERATOR_NAME or "TUI"

        # Resolve targets
        if target.lower() == "all":
            targets = KNOWN_AGENTS
        else:
            # Case-insensitive match against known agents
            matched = [a for a in KNOWN_AGENTS if a.lower() == target.lower()]
            if not matched:
                self.notify(
                    f"Unknown agent '{target}'. Known: {', '.join(KNOWN_AGENTS)}",
                    severity="error",
                )
                return
            targets = matched

        # Send via localmail
        try:
            sys.path.insert(0, '/home/eric/projects/agent-abide/core')
            from localmail import send_mail
            sent = []
            for agent in targets:
                send_mail(from_agent=from_name, to_agent=agent, text=message)
                sent.append(agent)
            m = AgentMessage(); content.mount(m)
            if len(sent) == 1:
                m.append_chunk(f"Mail sent to **{sent[0]}**: {message}")
            else:
                m.append_chunk(f"Mail broadcast to **{', '.join(sent)}**: {message}")
            self._scroll_to_bottom()
        except Exception as e:
            self.notify(f"Mail error: {e}", severity="error")

    def _ensure_adapter_attached(self, adapter: str) -> None:
        """Ensure adapter is in direct_attach. Bootstrap if missing."""
        try:
            with open(Config.awareness_file()) as f:
                awareness = json.load(f)
            if adapter not in awareness.get("direct_attach", []):
                awareness.setdefault("direct_attach", []).append(adapter)
                with open(Config.awareness_file(), "w") as f:
                    json.dump(awareness, f, indent=2)
                self.notify(f"Added '{adapter}' to direct_attach", severity="information")
        except Exception:
            pass

    def _set_gaze_to_room(self, room: str) -> None:
        """Set gaze by writing gaze.json directly + command queue."""
        agent_lower = self._active_agent.lower()
        if room == "tui":
            self._ensure_adapter_attached("tui")
            gaze = {
                "speech": {"target": "tui", "params": {}},
                "thoughts": {"target": "irc", "params": {"room": f"#{agent_lower}-thoughts"}}
            }
            gaze_str = "tui"
        elif room.startswith("pm:"):
            pm_target = room[3:]
            gaze = {
                "speech": {"target": "irc", "params": {"room": room}},
                "thoughts": {"target": "irc", "params": {"room": f"#{agent_lower}-thoughts"}}
            }
            gaze_str = f"irc/{room}"
        else:
            # Check if room matches a known non-IRC adapter
            adapter_dir = Path(Config.AGENTS_HOME) / self._active_agent / "asdaaas" / "adapters" / room
            if adapter_dir.exists() and room != "irc":
                self._ensure_adapter_attached(room)
                gaze = {
                    "speech": {"target": room, "params": {}},
                    "thoughts": {"target": "irc", "params": {"room": f"#{agent_lower}-thoughts"}}
                }
                gaze_str = room
            else:
                gaze = {
                    "speech": {"target": "irc", "params": {"room": room}},
                    "thoughts": {"target": "irc", "params": {"room": f"#{agent_lower}-thoughts"}}
                }
                gaze_str = f"irc/{room}"

        try:
            with open(Config.gaze_file(), "w") as f:
                json.dump(gaze, f)
            self.notify(f"Gaze set to {gaze_str}", severity="information")
        except Exception as e:
            self.notify(f"Failed to write gaze: {e}", severity="error")

    def _show_status_info(self, content: Vertical):
        """Show agent status."""
        info = []
        # Read signals
        signals_path = Config.find_signals_file()
        if signals_path and signals_path.exists():
            try:
                with open(signals_path) as f:
                    signals = json.load(f)
                info.append(f"**Context:** {signals.get('contextWindowUsage', '?')}%")
                info.append(f"**Tokens:** {signals.get('contextTokensUsed', '?')} / {signals.get('contextWindowTokens', '?')}")
                info.append(f"**Compactions:** {signals.get('compactionCount', 0)}")
            except Exception:
                info.append("*Could not read signals*")

        # Read health
        try:
            with open(Config.health_file()) as f:
                health = json.load(f)
            info.append(f"**Status:** {health.get('status', '?')}")
            info.append(f"**Last turn:** {health.get('last_turn_ts', '?')}")
        except Exception:
            info.append("*Could not read health*")

        msg = AgentMessage()
        content.mount(msg)
        msg.append_chunk("## Agent Status\n\n" + "\n".join(info))
        self._scroll_to_bottom()

    def _show_gaze_info(self, content: Vertical):
        """Show gaze info."""
        try:
            with open(Config.gaze_file()) as f:
                gaze = json.load(f)
            text = f"## Gaze\n\n```json\n{json.dumps(gaze, indent=2)}\n```"
        except Exception:
            text = "*Could not read gaze*"
        msg = AgentMessage()
        content.mount(msg)
        msg.append_chunk(text)
        self._scroll_to_bottom()

    def _show_awareness_info(self, content: Vertical):
        """Show current awareness configuration."""
        try:
            with open(Config.awareness_file()) as f:
                awareness = json.load(f)
            direct = awareness.get("direct_attach", [])
            channels = awareness.get("background_channels", {})
            lines = ["## Awareness\n"]
            lines.append(f"**Direct attach:** {', '.join(f'`{a}`' for a in direct) if direct else '*none*'}\n")
            lines.append("| Channel | Mode |")
            lines.append("|---------|------|")
            for ch, mode in sorted(channels.items()):
                lines.append(f"| `{ch}` | {mode} |")
            lines.append(f"\nDefault: **{awareness.get('background_default', 'pending')}**")
            lines.append(f"\nUse `/awareness add <channel> [doorbell|pending|drop]` or `/awareness rm <channel>`")
            text = "\n".join(lines)
        except Exception:
            text = "*Could not read awareness*"
        msg = AgentMessage()
        content.mount(msg)
        msg.append_chunk(text)
        self._scroll_to_bottom()

    def _show_health_info(self, content: Vertical):
        """Show health info."""
        try:
            with open(Config.health_file()) as f:
                health = json.load(f)
            text = f"## Health\n\n```json\n{json.dumps(health, indent=2)}\n```"
        except Exception:
            text = "*Could not read health*"
        msg = AgentMessage()
        content.mount(msg)
        msg.append_chunk(text)
        self._scroll_to_bottom()

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------

    def action_toggle_gaze_selector(self) -> None:
        """Toggle the gaze selector dropdown."""
        try:
            selector = self.query_one("#gaze-selector", GazeSelector)
            if selector.display:
                selector.display = False
                self.query_one("#input-bar", MessageInput).focus()
            else:
                selector.populate()
                selector.display = True
                selector.focus()
        except NoMatches:
            pass

    def action_dismiss_overlay(self) -> None:
        """Dismiss any open overlay, or focus input."""
        try:
            selector = self.query_one("#gaze-selector", GazeSelector)
            if selector.display:
                selector.display = False
                self.query_one("#input-bar", MessageInput).focus()
                return
        except NoMatches:
            pass
        try:
            slash_menu = self.query_one("#slash-menu", SlashMenu)
            if slash_menu.display:
                slash_menu.display = False
                self.query_one("#input-bar", MessageInput).focus()
                return
        except NoMatches:
            pass
        self.query_one("#input-bar", MessageInput).focus()

    def action_interrupt_agent(self) -> None:
        """Send an interrupt to the active agent (like Ctrl+C in grok TUI)."""
        agent_dir = Path(Config.AGENTS_HOME) / self._active_agent
        cmd_dir = agent_dir / "asdaaas" / "commands"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time() * 1000)
        rand = secrets.token_hex(4)
        cmd = {
            "action": "interrupt",
            "reason": "Operator pressed Ctrl+C in TUI",
        }
        cmd_path = cmd_dir / f"cmd_{ts}_{rand}.json"
        with open(cmd_path, "w") as f:
            json.dump(cmd, f)

        content = self._content_scroll()
        content.mount(HookAnnotation("⚡ Interrupt sent to agent"))
        self._scroll_to_bottom()
        self.notify(f"Interrupt sent to {self._active_agent}", severity="warning")

    def action_switch_agent(self, agent_name: str) -> None:
        """Switch to a different agent tab."""
        if agent_name not in self._agents:
            return
        if agent_name == self._active_agent and not self._room_active:
            return

        # Hide room content if switching from room
        if self._room_active:
            self._room_active = False
            try:
                room_scroll = self.query_one("#content-room", ContentScroll)
                room_scroll.display = False
            except NoMatches:
                pass

        # Hide current content
        try:
            current_scroll = self._content_scroll()
            current_scroll.display = False
        except NoMatches:
            pass

        # Switch active agent
        old_agent = self._active_agent
        self._active_agent = agent_name
        Config.AGENT_NAME = agent_name

        # Show new content
        try:
            new_scroll = self._content_scroll()
            new_scroll.display = True
        except NoMatches:
            pass

        # Update header
        try:
            header = self.query_one("#agent-header", AgentHeader)
            header.agent_name = agent_name
        except NoMatches:
            pass

        # Update tab bar
        try:
            tab_bar = self.query_one("#agent-tab-bar", AgentTabBar)
            tab_bar.active_agent = agent_name
        except NoMatches:
            pass

        # Save current draft, restore new agent's draft
        try:
            input_bar = self.query_one("#input-bar", MessageInput)
            # Save current agent's draft
            old_state = self._agent_state.get(old_agent)
            if old_state is not None:
                old_state["input_draft"] = input_bar.text
            # Restore new agent's draft
            input_bar.clear()
            new_draft = self._agent_state[agent_name].get("input_draft", "")
            if new_draft:
                input_bar.insert(new_draft)
            input_bar._placeholder = f"Message {agent_name}..."
        except NoMatches:
            pass

        # Scroll to bottom on tab switch and re-enable following
        try:
            new_scroll = self._content_scroll()
            new_scroll._follow_tail = True
            new_scroll.scroll_end(animate=False)
            self.set_timer(0.3, lambda: new_scroll.scroll_end(animate=False))
        except NoMatches:
            pass

        # No toast — tab bar already shows the active agent

    def action_next_agent(self) -> None:
        """Cycle to next agent tab (includes Room)."""
        tabs = self._agents + [AgentTabBar.ROOM_TAB]
        cur = AgentTabBar.ROOM_TAB if self._room_active else self._active_agent
        idx = tabs.index(cur) if cur in tabs else 0
        next_tab = tabs[(idx + 1) % len(tabs)]
        if next_tab == AgentTabBar.ROOM_TAB:
            self.action_switch_to_room()
        else:
            self.action_switch_agent(next_tab)

    def action_switch_to_room(self) -> None:
        """Switch to the IRC room tab."""
        # Hide current agent content
        if not self._room_active:
            try:
                current_scroll = self._content_scroll()
                current_scroll.display = False
            except NoMatches:
                pass

            try:
                input_bar = self.query_one("#input-bar", MessageInput)
                old_state = self._agent_state.get(self._active_agent)
                if old_state is not None:
                    old_state["input_draft"] = input_bar.text
                input_bar.clear()
                input_bar._placeholder = f"Message {self._room_channel}..."
            except NoMatches:
                pass

        # Show room content
        self._room_active = True
        try:
            room_scroll = self.query_one("#content-room", ContentScroll)
            room_scroll.display = True
            room_scroll._follow_tail = True
            room_scroll.scroll_end(animate=False)
        except NoMatches:
            pass

        # Update header
        try:
            header = self.query_one("#agent-header", AgentHeader)
            header.agent_name = self._room_channel
        except NoMatches:
            pass

        # Update tab bar
        try:
            tab_bar = self.query_one("#agent-tab-bar", AgentTabBar)
            tab_bar.active_agent = AgentTabBar.ROOM_TAB
        except NoMatches:
            pass

    def _connect_room_irc(self):
        """Establish and maintain the IRC connection for the Room tab."""
        import socket as _socket
        nick = Config.OPERATOR_NAME or "eric"
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", 6667))
        sock.sendall(f"NICK {nick}\r\n".encode())
        sock.sendall(f"USER {nick} 0 * :{nick}\r\n".encode())
        sock.sendall(f"JOIN {self._room_channel}\r\n".encode())
        # Drain registration replies
        sock.settimeout(1.0)
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                for line in data.decode(errors="replace").split("\r\n"):
                    if line.startswith("PING"):
                        pong = line.replace("PING", "PONG", 1)
                        sock.sendall(f"{pong}\r\n".encode())
        except _socket.timeout:
            pass
        sock.settimeout(5.0)
        self._room_irc_sock = sock
        self.run_worker(self._room_irc_keepalive, thread=True, name="room_keepalive")

    def _room_irc_keepalive(self):
        """Background thread: respond to IRC PINGs to keep connection alive."""
        import socket as _socket
        while self._room_irc_sock is not None:
            try:
                self._room_irc_sock.settimeout(30.0)
                data = self._room_irc_sock.recv(4096)
                if not data:
                    break
                for line in data.decode(errors="replace").split("\r\n"):
                    if line.startswith("PING"):
                        pong = line.replace("PING", "PONG", 1)
                        self._room_irc_sock.sendall(f"{pong}\r\n".encode())
            except _socket.timeout:
                continue
            except Exception:
                break

    def _send_to_room(self, text: str):
        """Send a message to the IRC room via persistent socket connection."""
        try:
            if self._room_irc_sock is None:
                self._connect_room_irc()
            self._room_irc_sock.sendall(
                f"PRIVMSG {self._room_channel} :{text}\r\n".encode()
            )
        except Exception as e:
            self._room_irc_sock = None
            self.call_from_thread(
                self._mount_room_system_msg, "", f"Send error: {e}"
            )

    def _tail_room_log(self) -> None:
        """Background thread: tail the IRC channel log file."""
        import re
        log_path = Path.home() / ".grok" / "irc_logs" / f"{self._room_channel}.log"

        msg_re = re.compile(r"^\[([^\]]+)\] <([^>]+)> (.*)$")
        action_re = re.compile(r"^\[([^\]]+)\] \* (\S+) (.*)$")

        # Wait for log file to exist
        while not log_path.exists():
            time.sleep(2)

        # Seek to tail (last 50 lines for scrollback)
        with open(log_path, "r") as f:
            lines = f.readlines()
            tail_lines = lines[-50:] if len(lines) > 50 else lines
            for line in tail_lines:
                line = line.rstrip()
                if not line:
                    continue
                m = msg_re.match(line)
                if m:
                    ts, nick, msg = m.groups()
                    short_ts = ts.split(" ")[1][:5] if " " in ts else ts[:5]
                    self.call_from_thread(
                        self._mount_room_msg, short_ts, nick, msg
                    )
                    continue
                m = action_re.match(line)
                if m:
                    ts, nick, action = m.groups()
                    short_ts = ts.split(" ")[1][:5] if " " in ts else ts[:5]
                    if any(w in action for w in ["joined", "quit", "left", "part"]):
                        self.call_from_thread(
                            self._mount_room_system_msg, short_ts, f"{nick} {action}"
                        )
                    else:
                        self.call_from_thread(
                            self._mount_room_msg, short_ts, nick, action, True
                        )
                    continue

            # Now tail for new lines
            f.seek(0, 2)  # seek to end
            worker = get_current_worker()
            while not worker.is_cancelled:
                line = f.readline()
                if not line:
                    time.sleep(0.3)
                    continue
                line = line.rstrip()
                if not line:
                    continue
                m = msg_re.match(line)
                if m:
                    ts, nick, msg = m.groups()
                    own_nick = (Config.OPERATOR_NAME or "eric").lower()
                    if nick.lower() == own_nick:
                        continue
                    short_ts = ts.split(" ")[1][:5] if " " in ts else ts[:5]
                    self.call_from_thread(
                        self._mount_room_msg, short_ts, nick, msg
                    )
                    continue
                m = action_re.match(line)
                if m:
                    ts, nick, action = m.groups()
                    short_ts = ts.split(" ")[1][:5] if " " in ts else ts[:5]
                    if any(w in action for w in ["joined", "quit", "left", "part"]):
                        self.call_from_thread(
                            self._mount_room_system_msg, short_ts, f"{nick} {action}"
                        )
                    else:
                        self.call_from_thread(
                            self._mount_room_msg, short_ts, nick, action, True
                        )

    def _mount_room_msg(self, ts: str, nick: str, msg: str, is_action: bool = False) -> None:
        """Mount a room message widget (called from main thread via call_from_thread)."""
        try:
            room_scroll = self.query_one("#content-room", ContentScroll)
            room_scroll.mount(RoomMessage(ts, nick, msg, is_action))
            if room_scroll._follow_tail:
                room_scroll.scroll_end(animate=False)
        except NoMatches:
            pass

    def _mount_room_system_msg(self, ts: str, msg: str) -> None:
        """Mount a room system message widget."""
        try:
            room_scroll = self.query_one("#content-room", ContentScroll)
            room_scroll.mount(RoomSystemMessage(ts, msg))
            if room_scroll._follow_tail:
                room_scroll.scroll_end(animate=False)
        except NoMatches:
            pass

    def action_clear_screen(self) -> None:
        """Clear the input bar (like Ctrl+L in grok binary)."""
        try:
            input_bar = self.query_one("#input-bar", MessageInput)
            input_bar.clear()
            input_bar.styles.height = 1
        except NoMatches:
            pass

    def action_focus_input(self) -> None:
        self.query_one("#input-bar", MessageInput).focus()

    def action_toggle_thinking(self) -> None:
        self._show_thinking = not self._show_thinking
        # Toggle visibility of all thinking blocks
        for widget in self.query("ThinkingBlock"):
            widget.display = self._show_thinking

    def action_scroll_bottom(self) -> None:
        try:
            scroll = self._content_scroll()
            scroll._follow_tail = True
            scroll.scroll_end(animate=False)
        except NoMatches:
            pass

    

    def action_show_persistence(self) -> None:
        """Show persistence management panel for the active agent."""
        agent_dir = Path.home() / "agents" / self._active_agent
        if agent_dir.exists():
            self.push_screen(PersistenceScreen(self._active_agent, agent_dir))

    def action_load_history(self) -> None:
        """Load older events (Page Up)."""
        self._load_older_history()

    def action_scroll_top(self) -> None:
        """Scroll to top and load older history if available."""
        self._load_older_history()
        try:
            scroll = self._content_scroll()
            scroll._follow_tail = False
            scroll.scroll_home(animate=False)
        except NoMatches:
            pass

    # -------------------------------------------------------------------------
    # Background workers
    # -------------------------------------------------------------------------

    def _poll_status_via_api(self, agent_name: str, header) -> Optional[int]:
        """Fetch health + gaze via API. Returns health_pct or None."""
        import urllib.request
        health_pct = None
        try:
            url = f"{Config.API_URL.rstrip('/')}/agents/{agent_name}/status"
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read())

            # Health
            health = data.get("health", {})
            if health:
                status = health.get("status", "unknown")
                self.call_from_thread(setattr, header, "health_status", status)
                generating = status == "working"
                self.call_from_thread(setattr, header, "is_generating", generating)
                try:
                    footer = self.query_one("#dynamic-footer", DynamicFooter)
                    self.call_from_thread(setattr, footer, "is_generating", generating)
                except NoMatches:
                    pass
                tokens = health.get("totalTokens")
                window = health.get("contextWindow")
                if isinstance(tokens, int) and isinstance(window, int) and window > 0:
                    health_pct = int(tokens / window * 100)
                    self.call_from_thread(setattr, header, "context_pct", health_pct)

            # Code version
            cv = health.get("code_version", "")
            if cv:
                self.call_from_thread(setattr, header, "code_version", cv)
                stale = bool(self._abide_head and cv != self._abide_head)
                self.call_from_thread(setattr, header, "code_version_stale", stale)

            # Gaze
            gaze = data.get("gaze", {})
            if gaze:
                speech = gaze.get("speech", {})
                target = speech.get("target", "?")
                params = speech.get("params", {})
                room = params.get("room", "")
                gaze_str = f"{target}/{room}" if room else target
                self.call_from_thread(setattr, header, "gaze_target", gaze_str)

        except Exception:
            pass
        return health_pct

    def _poll_health_filesystem(self, asdaaas_dir: Path, header) -> Optional[int]:
        """Read health.json from filesystem. Returns health_pct or None."""
        health_pct = None
        try:
            with open(asdaaas_dir / "health.json") as f:
                health = json.load(f)
            status = health.get("status", "unknown")
            self.call_from_thread(setattr, header, "health_status", status)
            generating = status == "working"
            self.call_from_thread(setattr, header, "is_generating", generating)
            try:
                footer = self.query_one("#dynamic-footer", DynamicFooter)
                self.call_from_thread(setattr, footer, "is_generating", generating)
            except NoMatches:
                pass
            tokens = health.get("totalTokens")
            window = health.get("contextWindow")
            if isinstance(tokens, int) and isinstance(window, int) and window > 0:
                health_pct = int(tokens / window * 100)
                self.call_from_thread(setattr, header, "context_pct", health_pct)
            cv = health.get("code_version", "")
            if cv:
                self.call_from_thread(setattr, header, "code_version", cv)
                stale = bool(self._abide_head and cv != self._abide_head)
                self.call_from_thread(setattr, header, "code_version_stale", stale)
        except Exception:
            pass
        return health_pct

    def _poll_gaze_filesystem(self, asdaaas_dir: Path, header) -> None:
        """Read gaze.json from filesystem."""
        try:
            with open(asdaaas_dir / "gaze.json") as f:
                gaze = json.load(f)
            speech = gaze.get("speech", {})
            target = speech.get("target", "?")
            params = speech.get("params", {})
            room = params.get("room", "")
            gaze_str = f"{target}/{room}" if room else target
            self.call_from_thread(setattr, header, "gaze_target", gaze_str)
        except Exception:
            pass

    def _poll_status(self) -> None:
        """Background thread: poll agent status for the active agent.

        When API_URL is set, uses GET /agents/{name}/status for health and gaze.
        Falls back to direct filesystem reads otherwise.
        """
        worker = get_current_worker()
        _head_refresh_counter = 0
        while not worker.is_cancelled:
            try:
                header = self.query_one("#agent-header", AgentHeader)
                active = self._active_agent
                agent_dir = Path(Config.AGENTS_HOME) / active
                asdaaas_dir = agent_dir / "asdaaas"

                # Refresh git HEAD every ~30 polls (~60s)
                _head_refresh_counter += 1
                if _head_refresh_counter >= 30:
                    self._abide_head = self._get_abide_head()
                    _head_refresh_counter = 0

                health_pct = None

                if Config.API_URL:
                    health_pct = self._poll_status_via_api(active, header)
                else:
                    health_pct = self._poll_health_filesystem(asdaaas_dir, header)
                    self._poll_gaze_filesystem(asdaaas_dir, header)

                # Read signals for context %
                try:
                    sessions_root = Config.sessions_root()
                    encoded = str(agent_dir).replace("/", "%2F")
                    session_dir = sessions_root / encoded
                    if session_dir.exists():
                        subdirs = [d for d in session_dir.iterdir() if d.is_dir()]
                        if subdirs:
                            latest = max(subdirs, key=lambda d: d.stat().st_mtime)
                            signals_path = latest / "signals.json"
                            if signals_path.exists():
                                with open(signals_path) as f:
                                    signals = json.load(f)
                                cc = signals.get("compactionCount", 0)
                                self.call_from_thread(
                                    setattr, header, "compaction_count", cc
                                )
                                # Only use signals.json context % as fallback
                                # (health.json is authoritative; signals lags on compaction)
                                if health_pct is None:
                                    pct = signals.get("contextWindowUsage", 0)
                                    self.call_from_thread(
                                        setattr, header, "context_pct", pct
                                    )
                                # Get model from summary.json (current_model_id) — more accurate than signals
                                summary_path = latest / "summary.json"
                                model = ""
                                if summary_path.exists():
                                    with open(summary_path) as sf:
                                        summary = json.load(sf)
                                    model = summary.get("current_model_id", "")
                                if not model:
                                    model = signals.get("primaryModelId", "")
                                if model:
                                    self.call_from_thread(
                                        setattr, header, "model_name", model
                                    )
                except Exception:
                    pass

                # Read compaction state
                try:
                    compact_path = asdaaas_dir / "compaction_state.json"
                    if compact_path.exists():
                        with open(compact_path) as f:
                            cstate = json.load(f)
                        phase = cstate.get("phase", "")
                        # Auto-hide complete phase after 2 minutes
                        if phase == "complete":
                            last_done = cstate.get("last_completed") or cstate.get("ts", 0)
                            if time.time() - last_done > 120:
                                phase = ""
                        # Build detail string for token savings
                        detail = ""
                        if phase == "complete" and cstate.get("tokens_before") and cstate.get("tokens_after"):
                            saved = cstate["tokens_before"] - cstate["tokens_after"]
                            if saved > 0:
                                detail = f"-{round(saved / 1000)}k"
                        self.call_from_thread(setattr, header, "compaction_phase", phase)
                        self.call_from_thread(setattr, header, "compaction_detail", detail)
                    else:
                        self.call_from_thread(setattr, header, "compaction_phase", "")
                        self.call_from_thread(setattr, header, "compaction_detail", "")
                except Exception:
                    pass

                # Read turn count from profile
                try:
                    profile_path = asdaaas_dir / "profile" / f"{active}.jsonl"
                    if profile_path.exists():
                        with open(profile_path, "rb") as f:
                            count = sum(1 for _ in f)
                        self.call_from_thread(
                            setattr, header, "turn_physical", count
                        )
                except Exception:
                    pass

                # Read delay pattern from most recent command
                try:
                    cmd_dir = asdaaas_dir / "commands"
                    if cmd_dir.exists():
                        cmd_files = sorted(cmd_dir.glob("cmd_*.json"), reverse=True)
                        delay_str = ""
                        for cf in cmd_files:
                            try:
                                with open(cf) as f:
                                    cmd = json.load(f)
                                if "action" in cmd and cmd["action"] == "delay":
                                    secs = cmd.get("seconds", "?")
                                    if secs == "until_event":
                                        delay_str = "wait"
                                    elif secs == 0:
                                        delay_str = "d:0"
                                    else:
                                        delay_str = f"d:{secs}s"
                                    break
                            except Exception:
                                continue
                        self.call_from_thread(
                            setattr, header, "delay_pattern", delay_str
                        )
                except Exception:
                    pass

            except NoMatches:
                pass
            except Exception:
                pass

            time.sleep(2)

    def _find_conversation_jsonl(self, agent_name: str) -> Optional[Path]:
        """Find conversation.jsonl for an agent (written by asdaaas)."""
        convo = Path(Config.AGENTS_HOME) / agent_name / "asdaaas" / "conversation.jsonl"
        return convo if convo.exists() else None

    def _convo_to_event(self, entry: dict) -> Optional[dict]:
        """Convert a conversation.jsonl entry to updates.jsonl event format."""
        role = entry.get("role", "")
        content = entry.get("content", "")
        ts_raw = entry.get("ts", "")
        if not content:
            return None
        if role == "assistant":
            session_update = "agent_message_chunk"
        elif role == "user":
            session_update = "user_message_chunk"
        elif role == "thinking":
            session_update = "agent_thought_chunk"
        else:
            return None
        # Convert ISO timestamp to epoch (int) for _dispatch_event compatibility
        ts = 0
        if isinstance(ts_raw, (int, float)):
            ts = int(ts_raw)
        elif isinstance(ts_raw, str) and ts_raw:
            try:
                ts = int(datetime.datetime.fromisoformat(ts_raw).timestamp())
            except (ValueError, OSError):
                ts = 0
        return {"timestamp": ts, "params": {"update": {
            "sessionUpdate": session_update,
            "content": {"text": content},
        }}}

    def _tail_conversation_jsonl(self, agent_name: str) -> None:
        """Background thread: tail conversation.jsonl for an agent."""
        worker = get_current_worker()
        state = self._agent_state[agent_name]
        convo_path = Path(Config.AGENTS_HOME) / agent_name / "asdaaas" / "conversation.jsonl"

        while not worker.is_cancelled and not convo_path.exists():
            time.sleep(2)
        if worker.is_cancelled:
            return

        is_primary = (agent_name == self._agents[0])
        tail_count = self._tail_count if is_primary else 30
        should_replay = (is_primary and self._replay_mode) or (not is_primary)

        offset = 0
        if should_replay:
            try:
                with open(convo_path, "r", errors="replace") as f:
                    all_data = f.read()
                    offset = f.tell()
                lines = [l for l in all_data.strip().split("\n") if l.strip()]
                if tail_count and len(lines) > tail_count:
                    lines = lines[-tail_count:]
                for line in lines:
                    try:
                        entry = json.loads(line)
                        event = self._convo_to_event(entry)
                        if event:
                            self.call_from_thread(self._finalize_current_msg_for, agent_name)
                            self.call_from_thread(self._dispatch_event_for_agent, event, agent_name)
                    except json.JSONDecodeError:
                        pass
                self.call_from_thread(self._finalize_current_msg_for, agent_name)
                self._debug(f"CONVO_REPLAY agent={agent_name} total_lines={len(lines)} tail={tail_count}")
                time.sleep(0.5)
                self.call_from_thread(self._force_scroll_bottom)
            except Exception as e:
                self._debug(f"CONVO_REPLAY_ERROR agent={agent_name}: {e}")
                pass
        else:
            try:
                offset = convo_path.stat().st_size
            except Exception:
                pass

        state["replay_done"] = True
        if is_primary:
            self._replay_done = True

        while not worker.is_cancelled:
            try:
                current_size = convo_path.stat().st_size
                if current_size > offset:
                    with open(convo_path, "r", errors="replace") as f:
                        f.seek(offset)
                        new_data = f.read()
                    if new_data.endswith("\n"):
                        offset += len(new_data.encode("utf-8"))
                        lines = new_data.strip().split("\n")
                    else:
                        last_nl = new_data.rfind("\n")
                        if last_nl == -1:
                            lines = []
                        else:
                            complete = new_data[:last_nl + 1]
                            offset += len(complete.encode("utf-8"))
                            lines = complete.strip().split("\n")
                    for line in lines:
                        if not line.strip():
                            continue
                        try:
                            entry = json.loads(line)
                            event = self._convo_to_event(entry)
                            if event:
                                self.call_from_thread(self._finalize_current_msg_for, agent_name)
                                self.call_from_thread(self._dispatch_event_for_agent, event, agent_name)
                        except json.JSONDecodeError:
                            pass
                elif current_size < offset:
                    offset = 0
            except FileNotFoundError:
                pass
            except Exception:
                pass
            time.sleep(0.3)

    def _finalize_current_msg_for(self, agent_name: str) -> None:
        """Reset message widgets so each conversation.jsonl entry renders fresh."""
        if agent_name == self._active_agent:
            self._current_agent_msg = None
            self._current_thinking = None

    def _find_updates_for_agent(self, agent_name: str) -> Optional[Path]:
        """Find updates.jsonl for a specific agent."""
        sessions_root = Config.sessions_root()
        agent_path = Path(Config.AGENTS_HOME) / agent_name
        encoded = str(agent_path).replace("/", "%2F")
        session_dir = sessions_root / encoded
        if session_dir.exists():
            subdirs = [d for d in session_dir.iterdir() if d.is_dir()]
            if subdirs:
                latest = max(subdirs, key=lambda d: d.stat().st_mtime)
                updates = latest / "updates.jsonl"
                if updates.exists():
                    return updates
        return None

    def _tail_updates_for_agent(self, agent_name: str) -> None:
        """Background thread: tail updates.jsonl for a specific agent."""
        worker = get_current_worker()
        state = self._agent_state[agent_name]

        # Claude-backed agents don't produce updates.jsonl (grok binary artifact).
        # Skip tailing — the status poller still works via health.json/gaze.json.
        if state.get("backend") == "claude":
            return

        # Find updates file for this agent
        if agent_name == self._agents[0]:
            # Primary agent uses Config
            updates_path = Config.find_updates_file()
        else:
            updates_path = self._find_updates_for_agent(agent_name)

        if not updates_path:
            while not worker.is_cancelled:
                updates_path = self._find_updates_for_agent(agent_name)
                if updates_path:
                    break
                time.sleep(5)

        if worker.is_cancelled or not updates_path:
            return

        # Cache the path for history loading
        state["updates_path"] = updates_path

        # Determine replay behavior
        is_primary = (agent_name == self._agents[0])
        # Non-primary agents always replay last 30 events for context
        should_replay = (is_primary and self._replay_mode) or (not is_primary)
        tail_count = self._tail_count if is_primary else 30

        if not should_replay:
            try:
                file_size = updates_path.stat().st_size
                state["updates_offset"] = file_size
                state["earliest_offset"] = file_size  # Allow loading history backwards
            except Exception:
                state["updates_offset"] = 0

        if should_replay:
            try:
                current_size = updates_path.stat().st_size
                if current_size > 0:
                    # For large files, only read the tail portion
                    if tail_count and current_size > 100000:
                        # Read last ~500KB to find enough lines
                        read_size = min(current_size, 500000)
                        seek_pos = current_size - read_size
                        with open(updates_path, "r", errors="replace") as f:
                            f.seek(seek_pos)
                            if seek_pos > 0:
                                f.readline()  # skip partial first line
                            data_start = f.tell()
                            tail_data = f.read()
                            state["updates_offset"] = f.tell()
                        all_lines = [l for l in tail_data.strip().split("\n") if l.strip()]
                        lines = all_lines[-tail_count:]
                        # Calculate earliest offset: skip the lines we didn't use
                        skipped_chars = sum(len(l) + 1 for l in all_lines[:-tail_count]) if len(all_lines) > tail_count else 0
                        state["earliest_offset"] = data_start + skipped_chars
                    else:
                        with open(updates_path, "r", errors="replace") as f:
                            all_data = f.read()
                            state["updates_offset"] = f.tell()
                        lines = [l for l in all_data.strip().split("\n") if l.strip()]
                        if tail_count and len(lines) > tail_count:
                            skipped = lines[:-tail_count]
                            skipped_chars = sum(len(l) + 1 for l in skipped)
                            state["earliest_offset"] = skipped_chars
                            lines = lines[-tail_count:]
                        else:
                            state["earliest_offset"] = 0
                    replay_count = 0
                    for line in lines:
                        try:
                            event = json.loads(line)
                            self.call_from_thread(
                                self._dispatch_event_for_agent, event, agent_name
                            )
                            replay_count += 1
                        except json.JSONDecodeError:
                            pass
                    self._debug(f"REPLAY dispatched={replay_count} lines={len(lines)}")
                    time.sleep(1)
                    self.call_from_thread(self._force_scroll_bottom)
            except Exception:
                pass
            state["replay_done"] = True
            self._replay_done = True

        while not worker.is_cancelled:
            try:
                current_size = updates_path.stat().st_size
                offset = state["updates_offset"]
                if current_size > offset:
                    with open(updates_path, "r", errors="replace") as f:
                        f.seek(offset)
                        new_data = f.read()

                    # Only process complete lines (ending with \n).
                    # A partial last line means grok is mid-write — leave it
                    # for the next poll by not advancing the offset past it.
                    if new_data.endswith("\n"):
                        state["updates_offset"] = offset + len(new_data.encode("utf-8"))
                        lines = new_data.strip().split("\n")
                    else:
                        last_nl = new_data.rfind("\n")
                        if last_nl == -1:
                            # No complete line yet — wait for more data
                            lines = []
                        else:
                            complete = new_data[:last_nl + 1]
                            state["updates_offset"] = offset + len(complete.encode("utf-8"))
                            lines = complete.strip().split("\n")

                    parsed_count = 0
                    skip_count = 0
                    for line in lines:
                        if not line.strip():
                            continue
                        try:
                            event = json.loads(line)
                            self.call_from_thread(
                                self._dispatch_event_for_agent, event, agent_name
                            )
                            parsed_count += 1
                        except json.JSONDecodeError:
                            skip_count += 1
                    self._debug(f"TAIL_POLL read={len(new_data)} lines={len(lines)} parsed={parsed_count} skipped={skip_count} offset={state['updates_offset']}")
                elif current_size < offset:
                    state["updates_offset"] = 0

            except FileNotFoundError:
                new_path = self._find_updates_for_agent(agent_name)
                if new_path:
                    updates_path = new_path
                    state["updates_offset"] = 0
            except Exception:
                pass

            time.sleep(0.1)

    def _tail_via_api(self, agent_name: str) -> None:
        """Background thread: tail agent messages via WebSocket API.

        Uses raw mode (full-fidelity JSONL events) so the existing
        _dispatch_event pipeline works unchanged. Tracks _tail_id for
        seamless reconnect without duplicate or missed events.
        """
        import websockets.sync.client as ws_sync

        worker = get_current_worker()
        state = self._agent_state[agent_name]
        api_url = Config.API_URL.rstrip("/")
        ws_url = api_url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/agents/{agent_name}/ws"

        is_primary = (agent_name == self._agents[0])
        should_replay = (is_primary and self._replay_mode) or (not is_primary)

        # Track last seen _tail_id for reconnect (-1 = start from end)
        last_tail_id = -1

        # --- Phase 1: Replay via REST if requested ---
        if should_replay:
            tail_n = self._tail_count if is_primary else 30
            try:
                import urllib.request
                rest_url = f"{api_url}/agents/{agent_name}/messages?last={tail_n}"
                with urllib.request.urlopen(rest_url, timeout=10) as resp:
                    data = json.loads(resp.read())
                replay_msgs = data.get("messages", [])
                replay_count = 0
                for msg in replay_msgs:
                    # REST returns normalized; use normalized dispatch for replay
                    self.call_from_thread(
                        self._dispatch_normalized_msg, msg, agent_name
                    )
                    replay_count += 1
                    # Track highest id for WS starting point
                    if "id" in msg:
                        last_tail_id = msg["id"]
                self._debug(f"API_REPLAY {agent_name} dispatched={replay_count}")
            except Exception as e:
                self._debug(f"API_REPLAY {agent_name} error: {e}")

            state["replay_done"] = True
            if is_primary:
                self._replay_done = True
            time.sleep(0.5)
            self.call_from_thread(self._force_scroll_bottom)

        # --- Phase 2: Live tail via WebSocket (raw mode) ---
        while not worker.is_cancelled:
            try:
                with ws_sync.connect(ws_url) as ws:
                    init = {"raw": True, "after_id": last_tail_id}
                    ws.send(json.dumps(init))
                    self._debug(f"WS {agent_name} connected, after_id={last_tail_id}")

                    while not worker.is_cancelled:
                        try:
                            data = ws.recv(timeout=2.0)
                        except TimeoutError:
                            continue

                        events = json.loads(data)
                        if not isinstance(events, list):
                            events = [events]

                        for event in events:
                            # Raw mode: full JSONL event with _tail_id
                            tid = event.pop("_tail_id", None)
                            self.call_from_thread(
                                self._dispatch_event_for_agent, event, agent_name
                            )
                            if tid is not None:
                                last_tail_id = tid

            except Exception as e:
                self._debug(f"WS {agent_name} error: {e}")
                if not worker.is_cancelled:
                    time.sleep(10)  # Longer backoff; agent may not have a session yet

    def _dispatch_normalized_msg(self, msg: dict, agent_name: str) -> None:
        """Dispatch a normalized API message to TUI widgets."""
        saved = self._active_agent
        self._active_agent = agent_name
        self._dispatching_agent = agent_name
        try:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content:
                return

            if role == "assistant":
                update = {"content": {"text": content}}
                self._on_agent_message_chunk(update)
            elif role == "thinking":
                update = {"content": {"text": content}}
                self._on_agent_thought_chunk(update)
            elif role == "user":
                update = {"content": {"text": content}}
                self._on_user_message_chunk(update)
            elif role == "tool_call":
                update = {"content": {"text": content}, "title": content[:60]}
                self._on_tool_call(update)
            elif role == "tool_result":
                update = {"content": [{"content": {"text": content}}]}
                self._on_tool_call_update(update)
        finally:
            self._active_agent = saved

    # -------------------------------------------------------------------------
    # Event dispatching
    # -------------------------------------------------------------------------

    def _dispatch_event_for_agent(self, event: dict, agent_name: str) -> None:
        """Dispatch an event, temporarily switching context to the target agent."""
        saved = self._active_agent
        self._active_agent = agent_name
        self._dispatching_agent = agent_name
        try:
            self._dispatch_event(event)
        finally:
            self._active_agent = saved
            self._dispatching_agent = None

    _debug_log = None  # Set to a file path to enable event logging

    @classmethod
    def enable_debug_log(cls, path="/tmp/tui_dispatch.log"):
        cls._debug_log = open(path, "w")

    def _debug(self, msg):
        if self._debug_log:
            import time as _t
            self._debug_log.write(f"{_t.time():.3f} {msg}\n")
            self._debug_log.flush()

    def _dispatch_event(self, event: dict) -> None:
        """Dispatch an updates.jsonl event to the appropriate renderer."""
        update = event.get("params", {}).get("update", {})
        event_type = update.get("sessionUpdate", "")
        # Stash event timestamp for turn separators
        self._last_event_ts = event.get("timestamp")

        if event_type == "agent_message_chunk":
            self._on_agent_message_chunk(update)
        elif event_type == "tool_call":
            self._on_tool_call(update)
        elif event_type == "tool_call_update":
            self._on_tool_call_update(update)
        elif event_type == "plan":
            self._on_plan(update)
        elif event_type == "hook_annotation":
            self._on_hook_annotation(update)
        elif event_type == "user_message_chunk":
            self._on_user_message_chunk(update)
        elif event_type == "agent_thought_chunk":
            self._on_agent_thought_chunk(update)
        elif event_type == "task_backgrounded":
            self._on_task_backgrounded(update)
        elif event_type == "task_completed":
            self._on_task_completed(update)
        elif event_type == "auto_compact_started":
            self._on_compact_started(update)
        elif event_type == "auto_compact_completed":
            self._on_compact_completed(update)
        elif event_type == "retry_state":
            self._on_retry_state(update)
        elif event_type == "doom_loop_detected":
            self._on_doom_loop(update)
        elif event_type == "available_commands_update":
            self._on_available_commands(update)
        # Silently ignore: git_branch_update, compaction_checkpoint

    def _on_agent_message_chunk(self, update: dict) -> None:
        """Handle streaming agent message text."""
        content_obj = update.get("content", {})
        text = content_obj.get("text", "")
        if not text:
            return

        content = self._content_scroll()

        was_none = self._current_agent_msg is None
        if was_none:
            self._current_agent_msg = AgentMessage()
            content.mount(self._current_agent_msg)
            content.refresh(layout=True)

        self._current_agent_msg.append_chunk(text)
        self._debug(f"MSG_CHUNK len={len(text)} total={len(self._current_agent_msg._text)} new_widget={was_none}")
        self._scroll_to_bottom()

    def _on_agent_thought_chunk(self, update: dict) -> None:
        """Handle thinking/reasoning chunks."""
        content_obj = update.get("content", {})
        text = content_obj.get("text", "")
        if not text:
            return

        content = self._content_scroll()

        if self._current_thinking is None:
            self._current_thinking = ThinkingBlock()
            self._current_thinking.display = self._show_thinking
            content.mount(self._current_thinking)
            content.refresh(layout=True)

        self._current_thinking.append_chunk(text)
        if self._show_thinking:
            self._scroll_to_bottom()

    def _on_tool_call(self, update: dict) -> None:
        """Handle new tool call announcement."""
        tool_id = update.get("toolCallId", "")
        title = update.get("title", "unknown tool")

        # End current agent message block (tool call is a boundary)
        self._current_agent_msg = None
        self._current_thinking = None

        content = self._content_scroll()
        panel = ToolCallPanel(tool_id, title)
        self._tool_panels[tool_id] = panel
        content.mount(panel)
        content.refresh(layout=True)
        self._debug(f"TOOL_CALL id={tool_id[:8]} title={title}")
        self._scroll_to_bottom()

    def _on_tool_call_update(self, update: dict) -> None:
        """Handle tool call status/output updates."""
        tool_id = update.get("toolCallId", "")
        status = update.get("status", "")
        kind = update.get("kind", "")
        title = update.get("title", "")
        content_list = update.get("content", [])

        panel = self._tool_panels.get(tool_id)

        if panel is None:
            # Tool call announcement might have been missed (e.g., started before TUI)
            # Create a panel for it
            display_title = title or f"tool {tool_id[:8]}"
            panel = ToolCallPanel(tool_id, display_title, kind)
            self._tool_panels[tool_id] = panel
            content = self._content_scroll()
            content.mount(panel)
            content.refresh(layout=True)

        # Update kind and title if provided
        if kind:
            panel.tool_kind = kind
        if title:
            panel.tool_title = title

        # Update status
        if status:
            panel.set_status(status)

        # Extract text content
        for item in content_list:
            if item.get("type") == "content":
                inner = item.get("content", {})
                text = inner.get("text", "")
                if text:
                    panel.set_output(text)
            elif item.get("type") == "diff":
                # Show diff info
                path = item.get("path", "")
                panel.set_output(f"[diff] {path}")

        out_len = len(panel.tool_output) if panel.tool_output else 0
        self._debug(f"TOOL_UPDATE id={tool_id[:8]} status={status} out_len={out_len} collapsed={panel._collapsed}")
        self._scroll_to_bottom()

    def _on_plan(self, update: dict) -> None:
        """Handle plan/todo updates."""
        entries = update.get("entries", [])
        if not entries:
            return

        content = self._content_scroll()

        # Remove previous plan panel if exists
        for widget in self.query("PlanPanel"):
            widget.remove()

        panel = PlanPanel(entries)
        content.mount(panel)
        self._scroll_to_bottom()

    def _on_hook_annotation(self, update: dict) -> None:
        """Handle hook annotation messages."""
        message = update.get("message", "")
        if not message:
            return

        content = self._content_scroll()
        content.mount(HookAnnotation(message))
        self._scroll_to_bottom()

    def _on_user_message_chunk(self, update: dict) -> None:
        """Handle user message display from updates stream."""
        content_obj = update.get("content", {})
        text = content_obj.get("text", "")
        if not text:
            return

        # Skip messages we just sent from the TUI input bar (avoid double-display)
        # Check if this text matches our last sent message
        if hasattr(self, "_last_sent_text") and self._last_sent_text and text.strip() == self._last_sent_text.strip():
            self._last_sent_text = None  # Clear so we only skip once
            return

        # Increment logical turn counter and insert separator
        state = self._agent_state[self._active_agent]
        state["logical_turn"] += 1
        turn_num = state["logical_turn"]
        trigger = classify_turn_trigger(text)

        # Format timestamp from update metadata or current time
        ts_str = ""
        meta = update.get("_meta", {})
        if meta.get("ts"):
            ts_str = meta["ts"]
        elif hasattr(self, "_last_event_ts") and self._last_event_ts:
            ts_str = datetime.datetime.fromtimestamp(
                self._last_event_ts
            ).strftime("%H:%M:%S")

        # End current agent message block
        self._current_agent_msg = None
        self._current_thinking = None

        content = self._content_scroll()
        content.mount(TurnSeparator(turn_num, trigger, ts_str))
        content.mount(UserMessage(text))
        content.refresh(layout=True)
        self._scroll_to_bottom()

        # Update header
        try:
            header = self.query_one("#agent-header", AgentHeader)
            header.turn_logical = turn_num
        except Exception:
            pass

    def _on_task_backgrounded(self, update: dict) -> None:
        """Handle task backgrounded notification."""
        task_id = update.get("task_id", "?")
        command = update.get("command", "?")
        content = self._content_scroll()
        content.mount(HookAnnotation(f"⏳ Task backgrounded: {command[:60]}... (id: {task_id[:8]})"))
        self._scroll_to_bottom()

    def _on_task_completed(self, update: dict) -> None:
        """Handle task completed notification."""
        snapshot = update.get("task_snapshot", {})
        task_id = snapshot.get("task_id", "?")
        command = snapshot.get("command", "?")
        exit_code = snapshot.get("exit_code", "?")
        content = self._content_scroll()
        status = "✓" if exit_code == 0 else f"✗ (exit {exit_code})"
        content.mount(HookAnnotation(f"{status} Task completed: {command[:60]}... (id: {task_id[:8]})"))
        self._scroll_to_bottom()

    def _on_compact_started(self, update: dict) -> None:
        content = self._content_scroll()
        content.mount(HookAnnotation("🔄 Auto-compaction started..."))
        self._scroll_to_bottom()
        try:
            header = self.query_one(AgentHeader)
            header.compaction_phase = "in_flight"
            header.compaction_detail = ""
        except Exception:
            pass

    def _on_compact_completed(self, update: dict) -> None:
        content = self._content_scroll()
        content.mount(HookAnnotation("✅ Auto-compaction completed"))
        self._scroll_to_bottom()
        try:
            header = self.query_one(AgentHeader)
            header.compaction_phase = "complete"
        except Exception:
            pass

    def _on_retry_state(self, update: dict) -> None:
        """Handle API retry notifications."""
        retry_type = update.get("type", "retrying")
        attempt = update.get("attempt", "?")
        max_retries = update.get("max_retries", "?")
        reason = update.get("reason", "Unknown error")
        msg = f"Retry {attempt}/{max_retries}: {reason}"
        content = self._content_scroll()
        content.mount(SystemAlert(msg, severity="warning"))
        self._scroll_to_bottom()

    def _on_doom_loop(self, update: dict) -> None:
        """Handle doom loop detection alerts."""
        repeat_count = update.get("repeat_count", "?")
        tool_names = update.get("tool_names", [])
        message = update.get("message", "Doom loop detected")
        is_warning = update.get("is_warning", True)
        tools_str = ", ".join(tool_names) if tool_names else "unknown"
        msg = f"🔁 Doom loop: {tools_str} repeated {repeat_count}x — {message}"
        severity = "warning" if is_warning else "error"
        content = self._content_scroll()
        content.mount(SystemAlert(msg, severity=severity))
        self._scroll_to_bottom()

    def _on_available_commands(self, update: dict) -> None:
        """Store available slash commands for autocomplete."""
        commands = update.get("availableCommands", [])
        self._available_commands = commands

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _load_older_history(self, agent_name: str = None) -> None:
        """Load older events when user scrolls to top."""
        agent_name = agent_name or self._active_agent
        state = self._agent_state[agent_name]

        if state["loading_history"] or state["earliest_offset"] <= 0:
            return
        updates_path = state.get("updates_path")
        if not updates_path:
            return

        state["loading_history"] = True
        batch_size = 1  # Load 1 event per scroll tick

        try:
            # Read backwards from earliest_offset
            read_size = min(state["earliest_offset"], 500000)
            seek_pos = state["earliest_offset"] - read_size

            with open(updates_path, "r", errors="replace") as f:
                f.seek(seek_pos)
                if seek_pos > 0:
                    f.readline()  # skip partial line
                data_start = f.tell()
                chunk = f.read(state["earliest_offset"] - data_start)

            all_lines = [l for l in chunk.strip().split("\n") if l.strip()]
            lines = all_lines[-batch_size:]

            if lines:
                # Calculate new earliest offset
                skipped = all_lines[:-batch_size] if len(all_lines) > batch_size else []
                skipped_chars = sum(len(l) + 1 for l in skipped)
                state["earliest_offset"] = data_start + skipped_chars

                content = self._content_scroll(agent_name)
                first_child = content.children[0] if content.children else None

                # Build widgets directly instead of using _dispatch_event
                widgets_to_prepend = []
                for line in lines:
                    try:
                        event = json.loads(line)
                        update = event.get("params", {}).get("update", {})
                        event_type = update.get("sessionUpdate", "")
                        
                        if event_type == "agent_message_chunk":
                            text = update.get("content", {}).get("text", "")
                            if text:
                                msg = AgentMessage()
                                msg._text = text
                                msg._chunks = [text]
                                widgets_to_prepend.append(msg)
                        elif event_type == "user_message_chunk":
                            text = update.get("content", {}).get("text", "")
                            if text:
                                widgets_to_prepend.append(UserMessage(text))
                        elif event_type == "tool_call_update":
                            title = update.get("title", "tool")
                            status = update.get("status", "completed")
                            kind = update.get("kind", "")
                            tool_id = update.get("toolCallId", "")
                            panel = ToolCallPanel(tool_id, title, kind)
                            panel.tool_status = status
                            panel._collapsed = True
                            content_list = update.get("content", [])
                            for item in content_list:
                                if item.get("type") == "content":
                                    inner = item.get("content", {})
                                    panel.tool_output = inner.get("text", "")
                            widgets_to_prepend.append(panel)
                        elif event_type == "hook_annotation":
                            message = update.get("message", "")
                            if message:
                                widgets_to_prepend.append(HookAnnotation(message))
                    except json.JSONDecodeError:
                        pass

                # Mount at the top
                if widgets_to_prepend:
                    try:
                        if first_child is not None:
                            content.mount(*widgets_to_prepend, before=first_child)
                        else:
                            for w in widgets_to_prepend:
                                content.mount(w)
                    except Exception as e:
                        self.notify(f"Mount error: {e}", severity="error")
                        # Fallback: append at end
                        for w in widgets_to_prepend:
                            try:
                                content.mount(w)
                            except Exception:
                                pass

                if state["earliest_offset"] <= 0:
                    self.notify("Reached beginning of session", severity="information")
        except Exception as e:
            self.notify(f"History error: {e}", severity="error")
        finally:
            state["loading_history"] = False

    _pending_scroll_timer = None

    _dispatching_agent = None

    def _scroll_to_bottom(self) -> None:
        """Scroll the content area to the bottom (only for the visible agent).

        Debounced: multiple calls in rapid succession result in a single
        deferred scroll, giving Textual's compositor time to recalculate
        layout before we read virtual_size.

        Skips scrolling if the event came from a background agent's tailer
        (prevents Sr's activity from snapping Trip's scroll position).
        """
        if self._replay_mode and not self._replay_done:
            return
        # If dispatching for a non-visible agent, skip scroll
        if self._dispatching_agent and self._dispatching_agent != self._active_agent:
            return
        # Respect user's scroll position — don't snap back if they scrolled up
        try:
            scroll = self._content_scroll()
            if not scroll._follow_tail:
                return
        except NoMatches:
            return
        if self._pending_scroll_timer is None:
            target = self._active_agent
            self._pending_scroll_timer = self.set_timer(
                0.05, lambda: self._do_deferred_scroll(target)
            )

    def _do_deferred_scroll(self, target_agent: str = None) -> None:
        """Execute the deferred scroll after compositor has updated layout."""
        self._pending_scroll_timer = None
        # If active agent changed since we scheduled (background dispatch restored
        # _active_agent after the timer was set), skip — wrong scroll target.
        if target_agent and target_agent != self._active_agent:
            return
        try:
            scroll = self._content_scroll()
            if scroll.display and scroll._follow_tail:
                scroll.scroll_end(animate=False)
                self.set_timer(0.2, self._follow_up_scroll)
        except NoMatches:
            pass

    def _follow_up_scroll(self) -> None:
        """Second scroll to catch layout changes from recently mounted widgets."""
        try:
            scroll = self._content_scroll()
            if scroll.display and scroll._follow_tail:
                scroll.scroll_end(animate=False)
        except NoMatches:
            pass

    def _force_scroll_bottom(self) -> None:
        """Force scroll to bottom -- used after replay completes."""
        try:
            scroll = self._content_scroll()
            scroll._follow_tail = True
            scroll.scroll_end(animate=False)
            # Also schedule a delayed one in case widgets are still mounting
            self.set_timer(0.5, lambda: scroll.scroll_end(animate=False))
            self.set_timer(2.0, lambda: scroll.scroll_end(animate=False))
        except NoMatches:
            pass


# =============================================================================
# CLI entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="asdaaas TUI — Full-screen development interface for agent sessions"
    )
    parser.add_argument(
        "--agent", "-a", default="Trip",
        help="Agent name (default: Trip)"
    )
    parser.add_argument(
        "--agents-home", default=os.path.expanduser("~/agents"),
        help="Agents home directory (default: ~/agents)"
    )
    parser.add_argument(
        "--updates", "-u", default=None,
        help="Path to updates.jsonl (auto-detected if not specified)"
    )
    parser.add_argument(
        "--replay", "-r", action="store_true",
        help="Replay existing updates.jsonl from the beginning (instead of tailing)"
    )
    parser.add_argument(
        "--tail", "-t", type=int, default=None,
        help="Only replay the last N events (use with --replay for fast startup)"
    )
    parser.add_argument(
        "--operator", "-o", default=None,
        help="Operator name (skips the 'Who are you?' prompt)"
    )
    parser.add_argument(
        "--sessions-dir", default=None,
        help="Grok sessions directory (default: auto-detect ~/.grok/sessions/ or .grok-users/)"
    )
    parser.add_argument(
        "--debug-log", default=None,
        help="Path to write dispatch debug log (e.g. /tmp/tui_dispatch.log)"
    )
    parser.add_argument(
        "--api-url", default=None,
        help="asdaaas API base URL (e.g. http://localhost:8420). Uses WebSocket for live tail."
    )
    args = parser.parse_args()

    Config.AGENT_NAME = args.agent
    Config.AGENTS_HOME = args.agents_home
    Config.UPDATES_FILE = args.updates
    Config.GROK_SESSIONS_DIR = args.sessions_dir
    Config.API_URL = args.api_url
    if args.operator:
        Config.OPERATOR_NAME = args.operator
        # Don't save to disk — --operator is ephemeral for test instances

    # Ensure adapter directories exist
    Config.tui_inbox().mkdir(parents=True, exist_ok=True)
    Config.tui_outbox().mkdir(parents=True, exist_ok=True)

    # Check for updates file (only needed when not using API)
    if not Config.API_URL:
        updates = Config.find_updates_file()
        if updates:
            print(f"Found updates at: {updates}")
        else:
            print(f"Warning: No updates.jsonl found for agent {Config.AGENT_NAME}")
            print("The TUI will wait for the file to appear...")

    # Discover agents from agents.json (authoritative) or fall back to filesystem
    agents_home = Path(Config.AGENTS_HOME)
    all_agents = []
    agents_json_path = Path(os.path.expanduser(
        "~/projects/agent-abide/agents.json"
    ))
    try:
        with open(agents_json_path) as f:
            agents_cfg = json.load(f)
        all_agents = [
            name for name in agents_cfg.get("agents", {})
            if (agents_home / name / "asdaaas").exists()
        ]
    except Exception:
        # Fallback: discover from filesystem
        if agents_home.exists():
            for d in sorted(agents_home.iterdir()):
                if d.is_dir() and (d / "asdaaas").exists():
                    all_agents.append(d.name)
    # Ensure primary agent is first
    if Config.AGENT_NAME in all_agents:
        all_agents.remove(Config.AGENT_NAME)
    all_agents.insert(0, Config.AGENT_NAME)

    app = AsdaaasTUI(agents=all_agents)

    if args.debug_log:
        AsdaaasTUI.enable_debug_log(args.debug_log)

    if args.replay or args.tail:
        app._replay_mode = True
    if args.tail:
        app._tail_count = args.tail

    app.run()


if __name__ == "__main__":
    main()
