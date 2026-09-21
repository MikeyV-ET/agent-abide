#!/usr/bin/env python3
"""
asdaaas_tui.py — Full-screen Textual TUI for asdaaas agent sessions.

Phase 1: Replicate the grok TUI development experience, routed through asdaaas.
The human operator should not be able to tell the difference from the real grok TUI.

Architecture:
  Input:  User types in InputBar → written to asdaaas TUI adapter inbox as JSON
  Output: Tails history/hot.jsonl (aa.stream) when present, else updates.jsonl → real-time
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
from rich.console import Console as RichConsole, Group

from ephact_parser import extract_ephacts, has_partial_ephact
from ephact_viewer import EphactViewer, archive_ephact, EphactEntry
from event_coalesce import coalesce_events
from chat_model import extract_interjections as _cm_extract_interjections, ChatState, apply_event, interjection_key, prune_items
from tui_env import TuiEnv
from theme import Theme, THEMES, set_theme, _save_theme, _load_saved_theme, apply_auto_if_needed, apply_theme_to_app
from chat_widgets import (
    ToolCallPanel, PlanPanel, UserMessage, AgentMessage, ThinkingBlock, InterjectionBlock,
    SystemReminderPanel, is_system_reminder,
)
from message_input import MessageInput
from chrome_widgets import (
    SystemAlert, ContentScroll, HookAnnotation, TurnSeparator, classify_turn_trigger,
)
from nav_widgets import (
    RoomMessage, RoomSystemMessage, AgentTabBar, AgentAddSelector,
    ThemeSelector, DynamicFooter,
)

from status_read import telemetry_from_files, code_version_stale


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
                # Config resolution: ASDAAAS_CONFIG env var, then repo root
                env = os.environ.get("ASDAAAS_CONFIG", "")
                ep = Path(env) if env else None
                if ep and ep.is_dir():
                    p = ep / "agents.json"
                elif ep and ep.is_file():
                    p = ep
                else:
                    # Prefer agents-home config, then repo-root agents.json
                    cand = [
                        Path(cls.AGENTS_HOME).parent / "config" / "agents.json",
                        Path(cls.AGENTS_HOME) / "config" / "agents.json",
                        Path(__file__).resolve().parent.parent / "agents.json",
                    ]
                    p = next((c for c in cand if c.exists()), cand[-1])
                with open(p) as f:
                    cls._agents_cfg = json.load(f)
            except Exception:
                cls._agents_cfg = {}
        return cls._agents_cfg

    @classmethod
    def list_catalog_agents(cls) -> list[str]:
        """Agents from agents.json that have an asdaaas directory (openable in TUI)."""
        cfg = cls.load_agents_cfg()
        agents_home = Path(cls.AGENTS_HOME)
        names: list[str] = []
        for name, acfg in (cfg.get("agents") or {}).items():
            if not isinstance(acfg, dict):
                continue
            home = Path(acfg.get("home", str(agents_home / name)))
            if (home / "asdaaas").exists():
                names.append(name)
        if not names:
            # filesystem fallback
            if agents_home.exists():
                for d in sorted(agents_home.iterdir()):
                    if d.is_dir() and (d / "asdaaas").exists():
                        names.append(d.name)
        return names

    @classmethod
    def agent_backend(cls, agent_name: str) -> str:
        """Return backend type for an agent ('grok' or 'claude')."""
        cfg = cls.load_agents_cfg()
        agent_cfg = cfg.get("agents", {}).get(agent_name, {})
        return agent_cfg.get("backend", "grok")

    @classmethod
    def agent_model(cls, agent_name: str) -> str:
        """Model id from agents.json, if configured."""
        cfg = cls.load_agents_cfg()
        agent_cfg = cfg.get("agents", {}).get(agent_name, {})
        return agent_cfg.get("model", "") or ""

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

    _env: "TuiEnv | None" = None  # type: ignore[assignment]

    @classmethod
    def get_env(cls) -> "TuiEnv":
        """Injectable path root; built from AGENTS_HOME when unset."""
        if cls._env is None:
            cls._env = TuiEnv.from_defaults(cls.AGENTS_HOME)
            # Prefer agents.json homes via agent_home still
        return cls._env

    @classmethod
    def set_env(cls, env: "TuiEnv") -> None:
        cls._env = env
        cls.AGENTS_HOME = str(env.agents_home)

    @classmethod
    def agent_home(cls, agent_name: str) -> Path:
        """Return home directory for an agent, using agents.json 'home' field.

        Falls back to AGENTS_HOME/agent_name if no 'home' field exists.
        """
        cfg = cls.load_agents_cfg()
        agent_cfg = cfg.get("agents", {}).get(agent_name, {})
        home = agent_cfg.get("home")
        if home:
            return Path(home)
        return cls.get_env().agent_home(agent_name)

    @classmethod
    def agent_dir(cls) -> Path:
        return cls.agent_home(cls.AGENT_NAME)

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
        if not session_dir.exists():
            return None
        # Prefer session ID from agents.json over mtime guess
        cfg = cls.load_agents_cfg()
        agent_cfg = cfg.get("agents", {}).get(cls.AGENT_NAME, {})
        sid = agent_cfg.get("session")
        if sid:
            updates = session_dir / sid / "updates.jsonl"
            if updates.exists():
                return updates
        # Fallback: most recent subdir (may pick subagent sessions)
        subdirs = [d for d in session_dir.iterdir() if d.is_dir()]
        if subdirs:
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
        if not session_dir.exists():
            return None
        cfg = cls.load_agents_cfg()
        agent_cfg = cfg.get("agents", {}).get(cls.AGENT_NAME, {})
        sid = agent_cfg.get("session")
        if sid:
            signals = session_dir / sid / "signals.json"
            if signals.exists():
                return signals
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
    tui_version = reactive("")  # this TUI process's checkout short hash

    def render(self) -> Text:
        text = Text()
        # Agent name
        text.append(f" {self.agent_name} ", style=f"bold {Theme.FG} on {Theme.DARK2}")
        text.append("  ")

        # Context usage with color coding
        pct = self.context_pct
        if pct < 50:
            ctx_style = Theme.BR_GREEN
        elif pct < 70:
            ctx_style = Theme.BR_YELLOW
        elif pct < 85:
            ctx_style = Theme.BR_ORANGE
        else:
            ctx_style = f"bold {Theme.BR_RED}"
        text.append("ctx: ", style=Theme.GRAY)
        text.append(f"{pct}%", style=ctx_style)

        if self.compaction_count > 0:
            text.append(f" (c:{self.compaction_count})", style=Theme.GRAY)

        # Compaction phase indicator
        cp = self.compaction_phase
        if cp in ("in_flight", "pending"):
            text.append(" ⟳ compacting", style=f"bold {Theme.BR_ORANGE}")
        elif cp == "complete" and self.compaction_detail:
            text.append(f" ✓ compacted {self.compaction_detail}", style=Theme.BR_GREEN)
        elif cp == "complete":
            text.append(" ✓ compacted", style=Theme.BR_GREEN)
        elif cp == "failed":
            text.append(" ✗ compact fail", style=f"bold {Theme.BR_RED}")

        text.append("  ")

        # Gaze (clickable)
        text.append("gaze: ", style=Theme.GRAY)
        text.append(f"[{self.gaze_target}]", style=f"bold underline {Theme.BR_AQUA}")
        text.append(" ▾", style=Theme.GRAY)
        text.append("  ")

        # Health + spinner
        h = self.health_status
        if self.is_generating:
            # Braille spinner animation
            spinner_frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            import time as _time
            frame = spinner_frames[int(_time.time() * 8) % len(spinner_frames)]
            text.append(frame, style=f"bold {Theme.BR_AQUA}")
            text.append(" generating", style=Theme.GRAY)
        elif h == "working":
            text.append("●", style=Theme.BR_GREEN)
            text.append(f" {h}", style=Theme.GRAY)
        elif h in ("ready", "active"):
            text.append("●", style=Theme.BR_AQUA)
            text.append(f" {h}", style=Theme.GRAY)
        elif h == "idle":
            text.append("○", style=Theme.BR_YELLOW)
            text.append(f" {h}", style=Theme.GRAY)
        elif h == "stalled":
            text.append("⚠", style=f"bold {Theme.BR_RED}")
            text.append(f" {h}", style=Theme.BR_RED)
        elif h == "restarting":
            text.append("↻", style=Theme.BR_YELLOW)
            text.append(f" {h}", style=Theme.GRAY)
        elif h == "error":
            text.append("✗", style=f"bold {Theme.BR_RED}")
            text.append(f" {h}", style=Theme.BR_RED)
        elif h in ("shutdown", "waiting"):
            text.append("◌", style=Theme.GRAY)
            text.append(f" {h}", style=Theme.GRAY)
        else:
            text.append("?", style=Theme.BR_RED)
            text.append(f" {h}", style=Theme.GRAY)

        # Turn reporting (physical turns = asdaaas-level prompt deliveries)
        if self.turn_physical > 0:
            text.append("  ")
            text.append("t:", style=Theme.GRAY)
            text.append(f"{self.turn_physical}", style=Theme.BR_AQUA)

        # Delay pattern
        if self.delay_pattern:
            text.append(" ")
            text.append(f"[{self.delay_pattern}]", style=Theme.DARK4)

        # Model name (right side)
        if self.model_name:
            text.append("  ")
            text.append(self.model_name, style=Theme.DARK4)

        # Code versions: agent asdaaas tip vs this TUI process (stale viewer catch)
        if self.code_version or self.tui_version:
            text.append("  ")
            if self.code_version:
                text.append("aa:", style=Theme.GRAY)
                if self.code_version_stale:
                    text.append(f"⚠{self.code_version}", style=f"bold {Theme.BR_YELLOW}")
                else:
                    text.append(self.code_version, style=Theme.DARK4)
            if self.tui_version:
                text.append(" tui:", style=Theme.GRAY)
                if self.code_version and self.tui_version != self.code_version:
                    text.append(f"⚠{self.tui_version}", style=f"bold {Theme.BR_YELLOW}")
                else:
                    text.append(self.tui_version, style=Theme.DARK4)

        return text

    def watch_is_generating(self, generating: bool) -> None:
        """Start/stop spinner refresh timer."""
        if generating:
            self._spinner_timer = self.set_interval(1 / 4, self.refresh)
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
            adapter_dir = Config.agent_home(Config.AGENT_NAME) / "asdaaas" / "adapters" / room
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
        {"name": "/theme", "description": "Change color theme"},
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

        text.append(f" Persistence: {agent} ", style=f"bold {Theme.FG} on {Theme.DARK2}")
        text.append("\n")

        # --- Health ---
        text.append("\n Health ", style=f"bold {Theme.BR_AQUA}")
        text.append("\n")
        health_path = d / "asdaaas" / "health.json"
        try:
            with open(health_path) as f:
                h = json.load(f)
            tokens = h.get("totalTokens", "?")
            ctx_win = h.get("contextWindow", "?")
            pct = int(tokens / ctx_win * 100) if isinstance(tokens, int) and isinstance(ctx_win, int) else "?"
            text.append(f"  Context: {tokens}/{ctx_win} ({pct}%)\n", style=Theme.FG)
            text.append(f"  Status: {h.get('status', '?')}  Detail: {h.get('detail', '')}\n", style=Theme.FG)
            text.append(f"  Model: {h.get('model', '?')}\n", style=Theme.FG)
            text.append(f"  Last activity: {h.get('last_activity', '?')}\n", style=Theme.FG)
        except Exception as e:
            text.append(f"  (could not read: {e})\n", style=Theme.BR_RED)

        # --- Lab Notebook ---
        text.append("\n Lab Notebook ", style=f"bold {Theme.BR_GREEN}")
        text.append("\n")
        notebook = d / f"lab_notebook_{agent.lower()}.md"
        if notebook.exists():
            import os as _os
            stat = _os.stat(notebook)
            size_kb = stat.st_size / 1024
            mtime = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
            text.append(f"  File: {notebook.name}\n", style=Theme.FG)
            text.append(f"  Size: {size_kb:.0f} KB  Modified: {mtime}\n", style=Theme.FG)
            # Find last ### entry header
            try:
                with open(notebook, "rb") as f:
                    f.seek(max(0, stat.st_size - 2000))
                    tail = f.read().decode("utf-8", errors="replace")
                entries = [l for l in tail.split("\n") if l.startswith("### ")]
                if entries:
                    text.append(f"  Last entry: {entries[-1][:60]}\n", style=Theme.FG)
            except Exception:
                pass
        else:
            text.append(f"  (not found: {notebook})\n", style=Theme.BR_RED)

        # --- Notes to Self ---
        text.append("\n Notes to Self ", style=f"bold {Theme.BR_YELLOW}")
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
            text.append(f"  File: {notes.name}\n", style=Theme.FG)
            text.append(f"  Size: {size_kb:.0f} KB  Modified: {mtime}\n", style=Theme.FG)
        else:
            text.append("  (not found)\n", style=Theme.DARK4)

        # --- Git ---
        text.append("\n Git ", style=f"bold {Theme.BR_PURPLE}")
        text.append("\n")
        try:
            import subprocess
            result = subprocess.run(
                ["git", "log", "--oneline", "-3", "--format=%h %ar  %s", "--", str(d)],
                cwd=str(d.parent), capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().split("\n")[:3]:
                    text.append(f"  {line}\n", style=Theme.FG)
            else:
                text.append("  (no recent commits)\n", style=Theme.DARK4)
        except Exception as e:
            text.append(f"  (git error: {e})\n", style=Theme.BR_RED)

        # --- Compaction History ---
        text.append("\n Compaction ", style=f"bold {Theme.BR_ORANGE}")
        text.append("\n")
        try:
            with open(health_path) as f:
                h = json.load(f)
            detail = h.get("detail", "")
            if "compacted" in detail:
                text.append(f"  {detail}\n", style=Theme.FG)
            else:
                text.append(f"  Detail: {detail}\n", style=Theme.FG)
        except Exception:
            text.append("  (unknown)\n", style=Theme.DARK4)

        # --- Session Profile ---
        text.append("\n Session ", style=f"bold {Theme.BR_BLUE}")
        text.append("\n")
        profile_dir = d / "asdaaas" / "profile"
        if profile_dir.exists():
            latest = profile_dir / f"{agent}_latest.json"
            jsonl = profile_dir / f"{agent}.jsonl"
            if jsonl.exists():
                try:
                    with open(jsonl, "rb") as f:
                        turn_count = sum(1 for _ in f)
                    text.append(f"  Physical turns: {turn_count}\n", style=Theme.FG)
                except Exception:
                    pass
            if latest.exists():
                try:
                    with open(latest) as f:
                        lp = json.load(f)
                    text.append(f"  Last turn: {lp.get('ts', '?')}\n", style=Theme.FG)
                    text.append(f"  Duration: {lp.get('wall_seconds', '?')}s\n", style=Theme.FG)
                except Exception:
                    pass

        # --- Doorbells ---
        bells_dir = d / "asdaaas" / "doorbells"
        if bells_dir.exists():
            bell_files = list(bells_dir.glob("*.json"))
            if bell_files:
                text.append(f"\n  Pending doorbells: {len(bell_files)}\n", style=Theme.BR_YELLOW)

        text.append("\n")
        text.append(" Press Esc or F2 to close ", style=Theme.DARK4)
        return text

    def action_dismiss_panel(self) -> None:
        self.dismiss(None)


# =============================================================================
# Main Application
# =============================================================================


# Long-run caps: Textual VerticalScroll is not virtualized. Every mounted child
# participates in layout. Without a window, multi-day sessions grow to 1000+
# widgets and hundreds of MB RSS, and the TUI stops responding to input.
MAX_SCROLLBACK_WIDGETS = 400
# Initial catch-up speech budget. Secondary tabs used to floor at 80 because
# lazy-load stalled on chrome walls / fat lines — that is fixed (scan_older +
# fat-line skip). Tip stays short; PageUp owns the rest.
DEFAULT_PRIMARY_TAIL_SPEECH = 50
DEFAULT_SECONDARY_TAIL_SPEECH = 25
# Prune check cadence (every N mounts/dispatches) to avoid remove thrash.
_PRUNE_EVERY_N = 8

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
    }

    #agent-header {
        height: 1;
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
        /* Was 12: multiline paste grew MessageInput past this and the child
           overflowed the dock (looked like an "escaped" full-screen text window).
           Cap at half the terminal so chat stays visible; TextArea scrolls. */
        max-height: 50%;
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
        border: round $accent-muted;
        padding: 0 1;
        border-title-align: left;
    }

    InterjectionBlock {
        border: round $warning;
        padding: 0 1;
        border-title-align: left;
    }

    SystemReminderPanel {
        margin: 0 0 0 2;
    }

    SystemAlert {
        margin: 0 0 0 0;
        height: auto;
    }

    #ephact-viewer {
        height: 40%;
        max-height: 50%;
        min-height: 10;
        margin: 0 0 0 0;
    }


    """

    BINDINGS = [
        Binding("ctrl+c", "interrupt_agent", "Interrupt", show=True),
        Binding("ctrl+q", "quit", "Quit", show=False),
        Binding("ctrl+l", "clear_screen", "Clear", show=True),
        Binding("ctrl+g", "toggle_gaze_selector", "Gaze", show=True),
        Binding("ctrl+t", "toggle_theme_selector", "Theme", show=True),
        Binding("escape", "dismiss_overlay", "Dismiss", show=False),
        Binding("f1", "toggle_thinking", "Toggle Thinking", show=True),
        Binding("f3", "close_ephact", "Artifact", show=True, priority=True),
        Binding("f5", "ephact_prev", "◀ Artifact", show=False, priority=True),
        Binding("f6", "ephact_next", "Artifact ▶", show=False, priority=True),

        Binding("end", "scroll_bottom", "Bottom", show=False),
        Binding("home", "scroll_top", "Top", show=False, priority=True),
        Binding("pageup", "load_history", "Load History", show=False, priority=True),
        Binding("ctrl+n", "next_agent", "Next Agent", show=False),
        Binding("f2", "show_persistence", "Persistence", show=True),
        Binding("f7", "toggle_copy_mode", "Copy Mode", show=True),
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
                "scroll_y": 0.0,  # Per-tab scroll position
                "follow_tail": True,  # Per-tab live-tail pin
                "input_draft": "",  # Saved input text when switching tabs
                "backend": Config.agent_backend(agent),  # "grok" or "claude"
                "logical_turn": 0,  # Logical turn counter (user_message_chunk events)
                "chat_state": ChatState(),  # pure model, dual-path with widgets
            }
        # Shared state
        self._abide_head = self._get_abide_head()
        self._replay_mode: bool = False
        self._replay_done: bool = False
        self._tail_count: Optional[int] = None
        self._show_thinking: bool = True
        self._available_commands: list[dict] = []
        self._seen_interjections: set[str] = set()  # global dedup by bell id / text

    @staticmethod
    def _get_abide_head() -> str:
        """Get current git HEAD of agent-abide repo. Returns short hash or ''."""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(Path(__file__).resolve().parent.parent),
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
            # Always show tab bar so [+] / × agent management is available
            yield AgentTabBar(self._agents, id="agent-tab-bar")
            yield AgentHeader(id="agent-header")
        yield GazeSelector(id="gaze-selector")
        yield ThemeSelector(id="theme-selector")
        yield AgentAddSelector(id="agent-add-selector")
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
        # Ephemeral artifact viewer — between chat and input for visual proximity
        viewer = EphactViewer(id="ephact-viewer")
        viewer.display = False
        yield viewer
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
        try:
            viewer = self.query_one("#ephact-viewer", EphactViewer)
            viewer.set_active_agent(self._active_agent)
        except NoMatches:
            pass
        try:
            tab_bar = self.query_one("#agent-tab-bar", AgentTabBar)
            tab_bar.active_agent = self._active_agent
        except NoMatches:
            pass
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
        # Room tab (IRC) — connect on first visit; optional log tail for history
        self._room_channel = "#meetingroom1"
        self._room_active = False
        self._room_irc_sock = None
        self._room_irc_nick = Config.OPERATOR_NAME or "eric"
        self._room_irc_buf = ""
        self._room_reader_started = False
        self._room_history_loaded_for = None
        self.run_worker(self._tail_room_log, thread=True, name="room_tailer")
        # Focus the input bar
        self.query_one("#input-bar", MessageInput).focus()
        # OS appearance poll (~5s) when theme preference is auto
        self.set_interval(5.0, self._poll_auto_theme)
        apply_theme_to_app(self)


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
        agent_dir = Config.agent_home(self._active_agent)
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
            st = self._agent_state.get(self._active_agent) or {}
            st["tool_panels"] = {}
            st["current_agent_msg"] = None
            st["current_thinking"] = None
            st["chat_state"] = ChatState()
            st["logical_turn"] = 0
            return
        elif cmd == "/status":
            self._show_status_info(content)
            return
        elif cmd == "/room":
            # Room tab: change IRC channel (/room #meetingroom1)
            if not arg.strip():
                self.notify(f"Current room: {self._room_channel}", severity="information")
                return
            if not self._room_active:
                self.action_switch_to_room()
            self._change_room_channel(arg.strip())
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
        elif cmd == "/theme":
            self.action_toggle_theme_selector()
            return
        elif cmd == "/help":
            help_text = """## TUI Commands

| Command | Description |
|---------|-------------|
| `/clear` | Clear the screen |
| `/status` | Show agent status |
| `/gaze [room]` | Show/set gaze target |
| `/room [#chan]` | Room tab: show/switch IRC channel |
| `/awareness` | Show/edit background channels |
| `/health` | Show health info |
| `/todo` | Manage persistent todo list |
| `/todo add <text>` | Add a todo item |
| `/todo done <n>` | Mark item n as done |
| `/todo rm <n>` | Remove item n |
| `/mail <agent> <msg>` | Send localmail to an agent |
| `/mail all <msg>` | Broadcast to all agents |
| `/whoami` | Show/change operator name |
| `/theme` | Change color theme |
| `/help` | Show this help |
| `Ctrl+C` | Interrupt agent |
| `Ctrl+Q` | Quit TUI |
| `Ctrl+L` | Clear screen |
| `Ctrl+G` | Gaze selector |
| `Ctrl+T` | Theme selector |
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
        return Config.agent_home(self._active_agent) / "tui_todos.json"

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
        cfg = Config.load_agents_cfg()
        agents_dict = cfg.get("agents", {})
        KNOWN_AGENTS = [
            name for name, acfg in agents_dict.items()
            if acfg.get("session")
        ]

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
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'core'))
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
            adapter_dir = Config.agent_home(self._active_agent) / "asdaaas" / "adapters" / room
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

        # Long-run scrollback telemetry (diagnose 7d hangs without a debugger)
        try:
            scroll = self._content_scroll()
            n_widgets = len(list(scroll.children))
            st = self._agent_state.get(self._active_agent) or {}
            cs = st.get("chat_state")
            n_items = len(cs.items) if cs is not None else 0
            n_panels = len(st.get("tool_panels") or {})
            follow = getattr(scroll, "_follow_tail", "?")
            info.append(
                f"**Scrollback:** {n_widgets} widgets "
                f"(cap {MAX_SCROLLBACK_WIDGETS}) · "
                f"ChatState {n_items} items · "
                f"{n_panels} tool panels · follow_tail={follow}"
            )
        except Exception:
            info.append("*Scrollback stats unavailable*")

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


    def _poll_auto_theme(self) -> None:
        """If theme is auto, follow OS light/dark (Grok-style ~5s poll)."""
        try:
            if apply_auto_if_needed():
                apply_theme_to_app(self)
                self.refresh(layout=True)
        except Exception:
            pass

    def action_toggle_theme_selector(self) -> None:
        """Toggle the theme selector dropdown."""
        try:
            selector = self.query_one("#theme-selector", ThemeSelector)
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
        """Dismiss any open overlay, exit copy mode, or focus input."""
        if getattr(self, '_copy_mode', False):
            self.action_toggle_copy_mode()
            return
        try:
            selector = self.query_one("#gaze-selector", GazeSelector)
            if selector.display:
                selector.display = False
                self.query_one("#input-bar", MessageInput).focus()
                return
        except NoMatches:
            pass
        try:
            selector = self.query_one("#theme-selector", ThemeSelector)
            if selector.display:
                selector.display = False
                self.query_one("#input-bar", MessageInput).focus()
                return
        except NoMatches:
            pass
        try:
            add_sel = self.query_one("#agent-add-selector", AgentAddSelector)
            if add_sel.display:
                add_sel.display = False
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
        # Close ephact viewer if open
        try:
            viewer = self.query_one("#ephact-viewer", EphactViewer)
            if viewer.display:
                viewer.close()
                self.query_one("#input-bar", MessageInput).focus()
                return
        except NoMatches:
            pass
        self.query_one("#input-bar", MessageInput).focus()

    def action_close_ephact(self) -> None:
        """Toggle the ephact viewer panel (show/hide)."""
        try:
            viewer = self.query_one("#ephact-viewer", EphactViewer)
            if viewer.display:
                viewer.close()
            elif viewer.has_content:
                viewer.display = True
                if hasattr(viewer, "_refresh_display"):
                    viewer._refresh_display()
                else:
                    viewer.refresh(layout=True)
        except NoMatches:
            pass

    def action_ephact_prev(self) -> None:
        """Navigate to older ephact in the stack."""
        try:
            viewer = self.query_one("#ephact-viewer", EphactViewer)
            if viewer.display:
                viewer.navigate(-1)
        except NoMatches:
            pass

    def action_ephact_next(self) -> None:
        """Navigate to newer ephact in the stack."""
        try:
            viewer = self.query_one("#ephact-viewer", EphactViewer)
            if viewer.display:
                viewer.navigate(1)
        except NoMatches:
            pass

    def action_interrupt_agent(self) -> None:
        """Send an interrupt to the active agent (like Ctrl+C in grok TUI)."""
        agent_dir = Config.agent_home(self._active_agent)
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


    def _refresh_header_for_agent(self, agent_name: str) -> None:
        """Immediately refresh top-bar telemetry for agent (tab switch).

        Uses pure status_read.telemetry_from_files — no 2s poller wait.
        """
        try:
            header = self.query_one("#agent-header", AgentHeader)
        except NoMatches:
            return
        asdaaas_dir = Config.agent_home(agent_name) / "asdaaas"
        tel = telemetry_from_files(
            agent_name,
            asdaaas_dir / "health.json",
            asdaaas_dir / "gaze.json",
            abide_head=getattr(self, "_abide_head", "") or "",
            model_fallback=Config.agent_model(agent_name),
        )
        header.agent_name = tel.agent_name
        header.health_status = tel.health_status
        header.is_generating = tel.is_generating
        header.context_pct = tel.context_pct
        header.code_version = tel.code_version
        header.tui_version = getattr(self, "_abide_head", "") or ""
        header.code_version_stale = code_version_stale(
            tel.code_version, getattr(self, "_abide_head", "") or ""
        )
        if tel.model_name:
            header.model_name = tel.model_name
        header.gaze_target = tel.gaze_target
        try:
            footer = self.query_one("#dynamic-footer", DynamicFooter)
            footer.is_generating = tel.is_generating
        except NoMatches:
            pass
        try:
            header.turn_logical = self._agent_state.get(agent_name, {}).get("logical_turn", 0)
        except Exception:
            pass

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

        # Update header telemetry immediately (don't wait for 2s poller)
        self._refresh_header_for_agent(agent_name)

        # Update tab bar
        try:
            tab_bar = self.query_one("#agent-tab-bar", AgentTabBar)
            tab_bar.active_agent = agent_name
        except NoMatches:
            pass

        # Switch ephact viewer to new agent's stack
        try:
            viewer = self.query_one("#ephact-viewer", EphactViewer)
            viewer.set_active_agent(agent_name)
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

        # Preserve each tab's scroll position (do not yank reader to tail/home).
        try:
            old_st = self._agent_state.get(old_agent)
            if old_st is not None:
                try:
                    old_scroll = self.query_one(f"#content-{old_agent}", ContentScroll)
                    old_st["scroll_y"] = float(getattr(old_scroll, "scroll_y", 0) or 0)
                    old_st["follow_tail"] = bool(getattr(old_scroll, "_follow_tail", True))
                except Exception:
                    pass
            new_scroll = self._content_scroll()
            new_st = self._agent_state.get(agent_name) or {}
            follow = new_st.get("follow_tail")
            if follow is None:
                follow = True
            new_scroll._follow_tail = bool(follow)
            if new_scroll._follow_tail:
                new_scroll.scroll_end(animate=False)
                self.set_timer(0.3, lambda s=new_scroll: s.scroll_end(animate=False))
            else:
                y = float(new_st.get("scroll_y") or 0)
                new_scroll.scroll_to(y=y, animate=False)
                self.set_timer(
                    0.3, lambda s=new_scroll, yy=y: s.scroll_to(y=yy, animate=False)
                )
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

    def _new_agent_state(self, agent: str) -> dict:
        return {
            "tool_panels": {},
            "current_agent_msg": None,
            "current_thinking": None,
            "updates_offset": 0,
            "replay_done": False,
            "earliest_offset": 0,
            "updates_path": None,
            "loading_history": False,
            "scroll_y": 0.0,
            "follow_tail": True,
            "input_draft": "",
            "backend": Config.agent_backend(agent),
            "logical_turn": 0,
            "chat_state": ChatState(),
            "removed": False,
        }

    def _sync_tab_bar(self) -> None:
        try:
            tab_bar = self.query_one("#agent-tab-bar", AgentTabBar)
            tab_bar.set_agents(self._agents)
            if self._room_active:
                tab_bar.active_agent = AgentTabBar.ROOM_TAB
            else:
                tab_bar.active_agent = self._active_agent
        except NoMatches:
            pass

    def _initial_tail_speech_count(self, agent_name: str) -> int:
        """Catch-up size for ``-t N``: N dialogue lines (+ collapsed tools).

        Tools in the span collapse to one line each and are capped so they
        cannot push chat out of the tip. PageUp lazy-load owns older history.
        """
        if self._tail_count:
            return max(1, int(self._tail_count))
        is_primary = bool(self._agents) and agent_name == self._agents[0]
        return DEFAULT_PRIMARY_TAIL_SPEECH if is_primary else DEFAULT_SECONDARY_TAIL_SPEECH

    def action_add_agent_menu(self) -> None:

        """Open picker of agents.json entries not already in the tab bar."""
        open_set = set(self._agents)
        candidates = [n for n in Config.list_catalog_agents() if n not in open_set]
        try:
            sel = self.query_one("#agent-add-selector", AgentAddSelector)
        except NoMatches:
            self.notify("Add-agent UI missing", severity="error")
            return
        # Hide other overlays
        for oid, cls in (("#gaze-selector", GazeSelector), ("#theme-selector", ThemeSelector)):
            try:
                w = self.query_one(oid, cls)
                w.display = False
            except NoMatches:
                pass
        sel.populate(candidates)
        sel.display = True
        sel.focus()

    def action_add_agent(self, agent_name: str) -> None:
        """Open an agent tab (from catalog) and start tailing it."""
        if not agent_name or agent_name in self._agents:
            return
        catalog = Config.list_catalog_agents()
        if agent_name not in catalog:
            self.notify(f"Unknown agent: {agent_name}", severity="warning")
            return

        self._agent_state[agent_name] = self._new_agent_state(agent_name)
        self._agents.append(agent_name)

        # Mount content scroll before room scroll
        vs = ContentScroll(id=f"content-{agent_name}")
        vs.display = False
        try:
            room = self.query_one("#content-room", ContentScroll)
            self.mount(vs, before=room)
        except NoMatches:
            self.mount(vs)

        # Start tail worker
        use_api = Config.API_URL is not None
        if use_api:
            self.run_worker(
                lambda a=agent_name: self._tail_via_api(a),
                thread=True, name=f"updates_{agent_name}",
            )
        else:
            self.run_worker(
                lambda a=agent_name: self._tail_updates_for_agent(a),
                thread=True, name=f"updates_{agent_name}",
            )

        self._sync_tab_bar()
        self.action_switch_agent(agent_name)
        self.notify(f"Added {agent_name}", severity="information", timeout=2)

    def action_remove_agent(self, agent_name: str) -> None:
        """Close an agent tab (does not delete agents.json entry)."""
        if agent_name not in self._agents:
            return
        if len(self._agents) <= 1:
            self.notify("Keep at least one agent open", severity="warning")
            return

        # Pick next active if removing current
        if self._active_agent == agent_name and not self._room_active:
            others = [a for a in self._agents if a != agent_name]
            self.action_switch_agent(others[0])

        # Mark removed so tail workers exit
        st = self._agent_state.get(agent_name)
        if st is not None:
            st["removed"] = True

        self._agents = [a for a in self._agents if a != agent_name]

        # Remove content widget
        try:
            scroll = self.query_one(f"#content-{agent_name}", ContentScroll)
            scroll.remove()
        except NoMatches:
            pass

        # Drop state (after workers can see removed flag)
        self._agent_state.pop(agent_name, None)

        self._sync_tab_bar()
        self.notify(f"Closed {agent_name}", severity="information", timeout=2)

    def action_switch_to_room(self) -> None:
        """Switch to the IRC room tab and ensure a live connection."""
        # Hide current agent content
        if not self._room_active:
            try:
                current_scroll = self._content_scroll()
                st = self._agent_state.get(self._active_agent)
                if st is not None:
                    st["scroll_y"] = float(getattr(current_scroll, "scroll_y", 0) or 0)
                    st["follow_tail"] = bool(getattr(current_scroll, "_follow_tail", True))
                current_scroll.display = False
            except NoMatches:
                pass

            try:
                input_bar = self.query_one("#input-bar", MessageInput)
                old_state = self._agent_state.get(self._active_agent)
                if old_state is not None:
                    old_state["input_draft"] = input_bar.text
                input_bar.clear()
                input_bar._placeholder = f"Message {self._room_channel}...  (/room #chan)"
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

        self._update_room_header()

        try:
            tab_bar = self.query_one("#agent-tab-bar", AgentTabBar)
            tab_bar.active_agent = AgentTabBar.ROOM_TAB
        except NoMatches:
            pass

        # Connect in background; always refresh recent history so we see
        # traffic that arrived while we were on agent tabs.
        self._room_history_loaded_for = None
        import threading
        threading.Thread(target=self._ensure_room_connected, args=(True,), daemon=True).start()

    def _update_room_header(self, status: str | None = None) -> None:
        """Show channel + connection status in the agent header."""
        try:
            header = self.query_one("#agent-header", AgentHeader)
            header.agent_name = self._room_channel
            if status is None:
                status = "connected" if self._room_irc_sock is not None else "disconnected"
            header.health_status = status
            header.gaze_target = f"irc/{self._room_channel}"
        except NoMatches:
            pass

    def _room_log_path(self) -> Path:
        # miniircd channel-log-dir uses channel name as filename including '#'
        return Path.home() / ".grok" / "irc_logs" / f"{self._room_channel}.log"

    def _ensure_room_connected(self, load_history: bool = False) -> bool:
        """Connect to local IRC if needed. Optionally load recent log history.

        Returns True if socket is ready for send/recv.
        """
        if self._room_irc_sock is not None:
            if load_history:
                self._load_room_history()
            return True
        ok = self._connect_room_irc()
        if ok and load_history:
            self._load_room_history()
        return ok

    def _try_start_irc_server(self) -> bool:
        """Best-effort start miniircd via launch script (localhost only)."""
        candidates = [
            Path(__file__).resolve().parent.parent / "scripts" / "launch_irc_server.sh",
            Path.home() / "projects" / "agent-abide" / "scripts" / "launch_irc_server.sh",
        ]
        script = next((c for c in candidates if c.exists()), None)
        if script is None:
            # Direct miniircd fallback
            mini = Path.home() / ".local" / "bin" / "miniircd"
            if not mini.exists():
                return False
            try:
                log_dir = Path.home() / ".grok" / "irc_logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                import subprocess
                subprocess.Popen(
                    [
                        "python3", str(mini),
                        "--listen", "127.0.0.1", "--ports", "6667",
                        "--channel-log-dir", str(log_dir),
                    ],
                    stdout=open("/tmp/miniircd.log", "a"),
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                time.sleep(0.8)
                return True
            except Exception:
                return False
        try:
            import subprocess
            subprocess.Popen(
                ["bash", str(script)],
                stdout=open("/tmp/miniircd_launch.log", "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            time.sleep(1.0)
            return True
        except Exception:
            return False

    def _connect_room_irc(self) -> bool:
        """Establish IRC connection for the Room tab. Returns success."""
        import socket as _socket
        nick = (Config.OPERATOR_NAME or "eric").replace(" ", "_")[:16]
        # Avoid empty / invalid nick
        if not nick or not nick[0].isalpha():
            nick = "op_" + (nick or "user")

        def _open() -> "_socket.socket":
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect(("127.0.0.1", 6667))
            sock.sendall(f"NICK {nick}\r\n".encode())
            sock.sendall(f"USER {nick} 0 * :{nick}\r\n".encode())
            sock.sendall(f"JOIN {self._room_channel}\r\n".encode())
            return sock

        try:
            sock = _open()
        except Exception as e1:
            self.call_from_thread(
                self._mount_room_system_msg,
                time.strftime("%H:%M"),
                f"IRC 127.0.0.1:6667 refused ({e1}). Starting miniircd…",
            )
            if not self._try_start_irc_server():
                self.call_from_thread(
                    self._mount_room_system_msg,
                    time.strftime("%H:%M"),
                    "Could not start IRC. Run: bash ~/projects/agent-abide-dev/scripts/launch_irc_server.sh",
                )
                self.call_from_thread(self._update_room_header, "disconnected")
                return False
            try:
                sock = _open()
            except Exception as e2:
                self.call_from_thread(
                    self._mount_room_system_msg,
                    time.strftime("%H:%M"),
                    f"Still cannot connect: {e2}",
                )
                self.call_from_thread(self._update_room_header, "disconnected")
                return False

        # Drain registration briefly (non-fatal)
        sock.settimeout(0.5)
        try:
            for _ in range(10):
                data = sock.recv(4096)
                if not data:
                    break
                self._handle_room_irc_bytes(data)
        except Exception:
            pass

        sock.settimeout(30.0)
        self._room_irc_sock = sock
        self._room_irc_nick = nick
        if not getattr(self, "_room_reader_started", False):
            self._room_reader_started = True
            self.run_worker(self._room_irc_reader, thread=True, name="room_irc_reader")

        self.call_from_thread(
            self._mount_room_system_msg,
            time.strftime("%H:%M"),
            f"Connected as {nick} → {self._room_channel}",
        )
        self.call_from_thread(self._update_room_header, "connected")
        return True

    def _handle_room_irc_bytes(self, data: bytes) -> None:
        """Parse IRC lines from socket data; update UI for channel traffic."""
        if not hasattr(self, "_room_irc_buf"):
            self._room_irc_buf = ""
        self._room_irc_buf += data.decode(errors="replace")
        while "\r\n" in self._room_irc_buf or "\n" in self._room_irc_buf:
            if "\r\n" in self._room_irc_buf:
                line, self._room_irc_buf = self._room_irc_buf.split("\r\n", 1)
            else:
                line, self._room_irc_buf = self._room_irc_buf.split("\n", 1)
            line = line.strip("\r")
            if not line:
                continue
            self._handle_room_irc_line(line)

    def _handle_room_irc_line(self, line: str) -> None:
        """Handle one IRC protocol line (PING, PRIVMSG, JOIN/PART, etc.)."""
        sock = self._room_irc_sock
        if line.startswith("PING"):
            if sock is not None:
                try:
                    pong = line.replace("PING", "PONG", 1)
                    if not pong.startswith("PONG"):
                        # PING :server
                        token = line.split(" ", 1)[1] if " " in line else ""
                        pong = f"PONG {token}"
                    sock.sendall(f"{pong}\r\n".encode())
                except Exception:
                    pass
            return

        # :nick!user@host PRIVMSG #chan :text
        # :nick!user@host JOIN :#chan
        prefix = ""
        rest = line
        if line.startswith(":"):
            try:
                prefix, rest = line[1:].split(" ", 1)
            except ValueError:
                return
        nick = prefix.split("!", 1)[0] if prefix else ""
        parts = rest.split(" ")
        if not parts:
            return
        cmd = parts[0].upper()
        ts = time.strftime("%H:%M")

        if cmd == "PRIVMSG" and len(parts) >= 2:
            target = parts[1]
            # message after first :
            msg = rest.split(" :", 1)[1] if " :" in rest else ""
            # Only show traffic for our room (or CTCP ignore)
            if target.lower() != self._room_channel.lower():
                return
            if msg.startswith("\x01") and msg.endswith("\x01"):
                # CTCP — skip ACTION handling lightly
                if msg.startswith("\x01ACTION ") and msg.endswith("\x01"):
                    action = msg[8:-1]
                    self.call_from_thread(self._mount_room_msg, ts, nick, action, True)
                return
            # Skip echo of our own messages if we already local-echoed
            own = (getattr(self, "_room_irc_nick", None) or Config.OPERATOR_NAME or "").lower()
            if nick.lower() == own:
                return
            self.call_from_thread(self._mount_room_msg, ts, nick, msg)
            return

        if cmd in ("JOIN", "PART", "QUIT", "NICK"):
            detail = rest
            if cmd == "JOIN":
                chan = parts[1].lstrip(":") if len(parts) > 1 else self._room_channel
                if chan.lower() != self._room_channel.lower() and not chan.startswith(":"):
                    # JOIN :#chan form
                    if " :" in rest:
                        chan = rest.split(" :", 1)[1]
                if self._room_channel.lower() not in (chan.lower(), f":{self._room_channel}".lower()):
                    if chan.lstrip(":").lower() != self._room_channel.lower():
                        return
                self.call_from_thread(self._mount_room_system_msg, ts, f"{nick} joined")
            elif cmd == "PART":
                self.call_from_thread(self._mount_room_system_msg, ts, f"{nick} left")
            elif cmd == "QUIT":
                self.call_from_thread(self._mount_room_system_msg, ts, f"{nick} quit")
            return

        # Nickname in use → try alternate
        if cmd == "433":
            alt = (getattr(self, "_room_irc_nick", "op") + "_tui")[:16]
            if sock is not None:
                try:
                    sock.sendall(f"NICK {alt}\r\n".encode())
                    self._room_irc_nick = alt
                except Exception:
                    pass

    def _room_irc_reader(self) -> None:
        """Background thread: read IRC socket, feed UI, answer PINGs."""
        import socket as _socket
        worker = get_current_worker()
        while not worker.is_cancelled:
            sock = self._room_irc_sock
            if sock is None:
                time.sleep(0.5)
                continue
            try:
                sock.settimeout(30.0)
                data = sock.recv(4096)
                if not data:
                    self._room_irc_sock = None
                    self.call_from_thread(
                        self._mount_room_system_msg,
                        time.strftime("%H:%M"),
                        "IRC disconnected",
                    )
                    self.call_from_thread(self._update_room_header, "disconnected")
                    time.sleep(1)
                    continue
                self._handle_room_irc_bytes(data)
            except _socket.timeout:
                continue
            except Exception as e:
                self._room_irc_sock = None
                self.call_from_thread(
                    self._mount_room_system_msg,
                    time.strftime("%H:%M"),
                    f"IRC error: {e}",
                )
                self.call_from_thread(self._update_room_header, "disconnected")
                time.sleep(1)

    def _send_to_room(self, text: str) -> None:
        """Send a message to the IRC room via persistent socket connection."""
        try:
            if not self._ensure_room_connected(load_history=False):
                self.call_from_thread(
                    self._mount_room_system_msg,
                    time.strftime("%H:%M"),
                    "Not connected — message not sent",
                )
                return
            sock = self._room_irc_sock
            if sock is None:
                return
            sock.sendall(
                f"PRIVMSG {self._room_channel} :{text}\r\n".encode()
            )
        except Exception as e:
            self._room_irc_sock = None
            self.call_from_thread(
                self._mount_room_system_msg, time.strftime("%H:%M"), f"Send error: {e}"
            )
            self.call_from_thread(self._update_room_header, "disconnected")

    def _load_room_history(self) -> None:
        """Load last lines from miniircd channel log into the room pane."""
        import re
        # Caller clears _room_history_loaded_for to force refresh (e.g. re-enter Room).
        if getattr(self, "_room_history_loaded_for", None) == self._room_channel:
            return
        log_path = self._room_log_path()
        if not log_path.exists():
            self._room_history_loaded_for = self._room_channel
            return
        msg_re = re.compile(r"^\[([^\]]+)\] <([^>]+)> (.*)$")
        action_re = re.compile(r"^\[([^\]]+)\] \* (\S+) (.*)$")
        try:
            with open(log_path, "r", errors="replace") as f:
                lines = f.readlines()
            tail_lines = lines[-80:] if len(lines) > 80 else lines
            self.call_from_thread(
                self._mount_room_system_msg,
                time.strftime("%H:%M"),
                f"— history ({len(tail_lines)} lines) —",
            )
            for line in tail_lines:
                line = line.rstrip()
                if not line:
                    continue
                m = msg_re.match(line)
                if m:
                    ts, nick, msg = m.groups()
                    short_ts = ts.split(" ")[1][:5] if " " in ts else ts[:5]
                    self.call_from_thread(self._mount_room_msg, short_ts, nick, msg)
                    continue
                m = action_re.match(line)
                if m:
                    ts, nick, action = m.groups()
                    short_ts = ts.split(" ")[1][:5] if " " in ts else ts[:5]
                    if any(w in action for w in ("joined", "quit", "left", "part")):
                        self.call_from_thread(
                            self._mount_room_system_msg, short_ts, f"{nick} {action}"
                        )
                    else:
                        self.call_from_thread(
                            self._mount_room_msg, short_ts, nick, action, True
                        )
        except Exception:
            pass
        self._room_history_loaded_for = self._room_channel

    def _change_room_channel(self, channel: str) -> None:
        """PART current channel, JOIN new one (must start with #)."""
        channel = channel.strip()
        if not channel:
            return
        if not channel.startswith("#"):
            channel = "#" + channel
        old = self._room_channel
        if channel == old:
            self.notify(f"Already in {channel}", severity="information")
            return
        sock = self._room_irc_sock
        if sock is not None:
            try:
                sock.sendall(f"PART {old}\r\n".encode())
                sock.sendall(f"JOIN {channel}\r\n".encode())
            except Exception:
                self._room_irc_sock = None
        self._room_channel = channel
        self._room_history_loaded_for = None
        try:
            input_bar = self.query_one("#input-bar", MessageInput)
            input_bar._placeholder = f"Message {channel}...  (/room #chan)"
        except NoMatches:
            pass
        self._update_room_header()
        self._mount_room_system_msg(time.strftime("%H:%M"), f"Switched to {channel}")
        import threading
        threading.Thread(target=self._ensure_room_connected, args=(True,), daemon=True).start()

    def _tail_room_log(self) -> None:
        """Optional: live-tail channel log (backup to socket reader)."""
        import re
        worker = get_current_worker()
        msg_re = re.compile(r"^\[([^\]]+)\] <([^>]+)> (.*)$")
        while not worker.is_cancelled:
            log_path = self._room_log_path()
            while not worker.is_cancelled and not log_path.exists():
                time.sleep(2)
                log_path = self._room_log_path()
            if worker.is_cancelled:
                return
            try:
                with open(log_path, "r", errors="replace") as f:
                    f.seek(0, 2)
                    while not worker.is_cancelled:
                        if log_path != self._room_log_path():
                            break  # channel changed — reopen
                        line = f.readline()
                        if not line:
                            time.sleep(0.3)
                            continue
                        line = line.rstrip()
                        m = msg_re.match(line)
                        if not m:
                            continue
                        ts, nick, msg = m.groups()
                        own = (getattr(self, "_room_irc_nick", None) or Config.OPERATOR_NAME or "").lower()
                        if nick.lower() == own:
                            continue
                        short_ts = ts.split(" ")[1][:5] if " " in ts else ts[:5]
                        self.call_from_thread(self._mount_room_msg, short_ts, nick, msg)
            except Exception:
                time.sleep(1)

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

    

    def action_toggle_copy_mode(self) -> None:
        """Toggle copy mode: disables mouse capture so terminal handles text selection."""
        self._copy_mode = not getattr(self, '_copy_mode', False)
        driver = self._driver
        if driver is None:
            return
        if self._copy_mode:
            driver._disable_mouse_support()
            self.notify("Copy mode ON — select text normally, F7 to exit", severity="information")
        else:
            driver._enable_mouse_support()
            self.notify("Copy mode OFF — mouse scrolling restored", severity="information")

    def action_show_persistence(self) -> None:
        """Show persistence management panel for the active agent."""
        agent_dir = Config.agent_home(self._active_agent)
        if agent_dir.exists():
            self.push_screen(PersistenceScreen(self._active_agent, agent_dir))

    def action_load_history(self) -> None:
        """Load older events (Page Up). Stay parked — do not re-enable tail follow."""
        try:
            self._content_scroll()._follow_tail = False
        except Exception:
            pass
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
                self.call_from_thread(
                    setattr, header, "tui_version", getattr(self, "_abide_head", "") or ""
                )
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
                self.call_from_thread(
                    setattr, header, "tui_version", getattr(self, "_abide_head", "") or ""
                )
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
                agent_dir = Config.agent_home(active)
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
                        cfg = Config.load_agents_cfg()
                        agent_cfg = cfg.get("agents", {}).get(active, {})
                        sid = agent_cfg.get("session")
                        signals_path = None
                        if sid:
                            sp = session_dir / sid / "signals.json"
                            if sp.exists():
                                signals_path = sp
                        if signals_path is None:
                            subdirs = [d for d in session_dir.iterdir() if d.is_dir()]
                            if subdirs:
                                latest = max(subdirs, key=lambda d: d.stat().st_mtime)
                                sp = latest / "signals.json"
                                if sp.exists():
                                    signals_path = sp
                        if signals_path is not None:
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

                # Read compaction state from binary_state.json (hook-written)
                try:
                    bstate_path = asdaaas_dir / "binary_state.json"
                    if bstate_path.exists():
                        with open(bstate_path) as f:
                            bstate = json.load(f)
                        cstate = bstate.get("compaction", {})
                        phase = cstate.get("phase", "")
                        # Auto-hide complete phase after 2 minutes
                        if phase == "complete":
                            last_done = cstate.get("completed_at", 0)
                            if time.time() - last_done > 120:
                                phase = ""
                        self.call_from_thread(setattr, header, "compaction_phase", phase)
                    else:
                        self.call_from_thread(setattr, header, "compaction_phase", "")
                except Exception:
                    pass

                # Read turn count from profile — cache by size to avoid re-scan every 2s
                try:
                    profile_path = asdaaas_dir / "profile" / f"{active}.jsonl"
                    if profile_path.exists():
                        st = profile_path.stat()
                        cache = getattr(self, "_profile_turn_cache", {})
                        key = (active, st.st_mtime_ns, st.st_size)
                        if key in cache:
                            count = cache[key]
                        else:
                            with open(profile_path, "rb") as f:
                                count = f.read().count(b"\n")
                            cache = {key: count}  # keep only latest
                            self._profile_turn_cache = cache
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
                        # Surface AA control in chat (prod parity)
                        try:
                            cs = asdaaas_dir / "control_state.json"
                            if cs.exists() and delay_str:
                                st = cs.stat()
                                key = (active, st.st_mtime_ns, st.st_size)
                                prev = getattr(self, "_control_state_key", None)
                                if key != prev:
                                    self._control_state_key = key
                                    data = json.loads(cs.read_text())
                                    content = data.get("content") or f"[aa.control] delay: {delay_str}"
                                    self.call_from_thread(
                                        self._mount_aa_control_line, content
                                    )
                        except Exception:
                            pass
                except Exception:
                    pass

            except NoMatches:
                pass
            except Exception:
                pass

            time.sleep(2)

    def _find_conversation_jsonl(self, agent_name: str) -> Optional[Path]:
        """Find conversation.jsonl for an agent (written by asdaaas)."""
        convo = Config.agent_home(agent_name) / "asdaaas" / "conversation.jsonl"
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
        convo_path = Config.agent_home(agent_name) / "asdaaas" / "conversation.jsonl"

        while not worker.is_cancelled and not convo_path.exists():
            time.sleep(2)
        if worker.is_cancelled:
            return

        is_primary = (agent_name == self._agents[0])
        # Tip only: same -t for every tab; no secondary floor. Lazy-load owns depth.
        tail_count = self._initial_tail_speech_count(agent_name)
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
        """Find grok updates.jsonl for a specific agent (legacy native path)."""
        sessions_root = Config.sessions_root()
        agent_path = Config.agent_home(agent_name)
        encoded = str(agent_path).replace("/", "%2F")
        session_dir = sessions_root / encoded
        if not session_dir.exists():
            return None
        cfg = Config.load_agents_cfg()
        agent_cfg = cfg.get("agents", {}).get(agent_name, {})
        sid = agent_cfg.get("session")
        if sid:
            updates = session_dir / sid / "updates.jsonl"
            if updates.exists():
                return updates
        subdirs = [d for d in session_dir.iterdir() if d.is_dir()]
        if subdirs:
            latest = max(subdirs, key=lambda d: d.stat().st_mtime)
            updates = latest / "updates.jsonl"
            if updates.exists():
                return updates
        return None

    def _resolve_display_history(self, agent_name: str) -> tuple[str, Optional[Path]]:
        """Prefer asdaaas/history/hot.jsonl; fall back to updates.jsonl.

        Returns (kind, path) kind in hot|updates|none.
        Env TUI_HISTORY_SOURCE=hot|updates|auto (default auto).
        """
        try:
            core = str(Path(__file__).resolve().parent.parent / "core")
            if core not in sys.path:
                sys.path.insert(0, core)
            from tui_history import resolve_history_source, hot_jsonl_path
        except Exception:
            # fallback: updates only
            if agent_name == self._agents[0]:
                p = Config.find_updates_file()
            else:
                p = self._find_updates_for_agent(agent_name)
            return ("updates", p) if p else ("none", None)

        home = Config.agent_home(agent_name)
        prefer = os.environ.get("TUI_HISTORY_SOURCE") or "auto"
        # Explicit --updates CLI forces updates for primary
        if agent_name == self._agents[0] and Config.UPDATES_FILE:
            return ("updates", Path(Config.UPDATES_FILE))

        kind, path = resolve_history_source(home, prefer=prefer)
        if kind == "hot" and path and path.exists():
            return ("hot", path)
        # updates: try tui_history candidates then grok sessions
        if kind == "updates" and path and path.exists():
            return ("updates", path)
        if agent_name == self._agents[0]:
            up = Config.find_updates_file()
        else:
            up = self._find_updates_for_agent(agent_name)
        if up:
            return ("updates", up)
        # last chance: empty hot path may appear later
        hot = hot_jsonl_path(home)
        return ("hot" if prefer == "hot" else "none", hot if prefer == "hot" else None)

    def _tail_updates_for_agent(self, agent_name: str) -> None:
        """Background thread: tail display history (hot.jsonl or updates.jsonl)."""
        worker = get_current_worker()
        state = self._agent_state[agent_name]

        kind, updates_path = self._resolve_display_history(agent_name)

        # Claude-backed: no grok updates.jsonl. hot.jsonl is the path once
        # stream_adapters.claude / full_stream_hook populate it.
        if state.get("backend") == "claude" and kind != "hot":
            # Wait briefly for hot to appear (adapter may start after TUI)
            while not worker.is_cancelled:
                kind, updates_path = self._resolve_display_history(agent_name)
                if kind == "hot" and updates_path and updates_path.exists():
                    break
                time.sleep(5)
            if worker.is_cancelled or kind != "hot" or not updates_path:
                return

        if not updates_path or not Path(updates_path).exists():
            while not worker.is_cancelled:
                kind, updates_path = self._resolve_display_history(agent_name)
                if updates_path and Path(updates_path).exists():
                    break
                time.sleep(5)

        if worker.is_cancelled or not updates_path:
            return

        updates_path = Path(updates_path)
        state["history_kind"] = kind
        # Cache the path for history loading
        state["updates_path"] = updates_path

        # Determine replay behavior
        is_primary = (agent_name == self._agents[0])
        # Non-primary always replays a short tip; PageUp lazy-load for older.
        should_replay = (is_primary and self._replay_mode) or (not is_primary)
        tail_count = self._initial_tail_speech_count(agent_name)

        if not should_replay:
            try:
                file_size = updates_path.stat().st_size
                state["updates_offset"] = file_size
                state["earliest_offset"] = file_size  # Allow loading history backwards
                info = self._scan_updates_for_latest_aa_control(updates_path)
                if info:
                    content = info.get("content") if isinstance(info, dict) else info
                    speech_after = (
                        bool(info.get("speech_after"))
                        if isinstance(info, dict)
                        else False
                    )
                    if content and speech_after:
                        self.call_from_thread(self._apply_delay_header, content)
                    elif content:
                        self.call_from_thread(
                            self._mount_aa_control_from_catchup, content
                        )
            except Exception:
                state["updates_offset"] = 0

        if should_replay:
            try:
                current_size = updates_path.stat().st_size
                hist_kind = state.get("history_kind") or kind
                if current_size > 0:
                    # Binary tail window. hot.jsonl lines embed full native payloads
                    # (often multi-KB); expand until we have enough *speech* events
                    # for -t N (or hit ceiling).
                    core = str(Path(__file__).resolve().parent.parent / "core")
                    if core not in sys.path:
                        sys.path.insert(0, core)
                    from tui_history import (
                        line_to_tui_event,
                        thin_tip_events,
                        select_tip_paint_lines,
                        is_tip_paint_event,
                        is_speech_tui_event,
                        is_dialogue_speech,
                    )

                    # -t N = N TUI paint lines (widgets), not N dialogue turns
                    want = int(tail_count) if tail_count else None
                    # grow window until thin tip can fill want lines (small first —
                    # Squiggy hot is 200MB+; do not read 128MiB for 50 lines)
                    windows = [0.25, 1, 2, 8, 32]
                    if hist_kind != "hot":
                        windows = [0.25, 1, 2, 8]
                    events: list = []
                    data_start = 0
                    raw_line_n = 0
                    for mib in windows:
                        read_size = min(current_size, int(mib * 1024 * 1024))
                        seek_pos = max(0, current_size - read_size)
                        with open(updates_path, "rb") as f:
                            f.seek(seek_pos)
                            if seek_pos > 0:
                                f.readline()  # drop partial
                            data_start = f.tell()
                            raw = f.read()
                            state["updates_offset"] = f.tell()
                        text_data = raw.decode("utf-8", errors="replace")
                        # (byte_offset, line) for earliest_offset after select
                        paired = []
                        pos = data_start
                        parts = text_data.split("\n")
                        for i, l in enumerate(parts):
                            line_start = pos
                            pos += len(l.encode("utf-8")) + (1 if i < len(parts) - 1 else 0)
                            if not l.strip():
                                continue
                            paired.append((line_start, l))
                        raw_line_n = len(paired)
                        events = []  # list of (offset, event)
                        for off, line in paired:
                            ev = line_to_tui_event(line, hist_kind)
                            if ev is not None:
                                events.append((off, ev))
                        # Need enough *dialogue* in the window to fill -t N.
                        only_probe = [e for _, e in events]
                        dialogue_n = sum(1 for e in only_probe if is_dialogue_speech(e))
                        tip_probe = (
                            select_tip_paint_lines(only_probe, want)
                            if want
                            else only_probe
                        )
                        paint_n = len(tip_probe)
                        tip_dialogue = sum(
                            1 for e in tip_probe if is_dialogue_speech(e)
                        )
                        self._debug(
                            f"REPLAY_WINDOW kind={hist_kind} mib={mib} "
                            f"raw={raw_line_n} events={len(events)} "
                            f"dialogue={dialogue_n} tip_dialogue={tip_dialogue} "
                            f"tip_paint={paint_n}"
                        )
                        if want is None or tip_dialogue >= want or seek_pos == 0:
                            break

                    # -t N = N TUI paint lines
                    only_ev = [e for _, e in events]
                    if want:
                        only_ev = select_tip_paint_lines(only_ev, want)
                        if only_ev:
                            want_ids = {id(e) for e in only_ev}
                            events = [(o, e) for o, e in events if id(e) in want_ids]
                        else:
                            events = []
                    if events:
                        state["earliest_offset"] = events[0][0]
                    else:
                        state["earliest_offset"] = data_start

                    replay_count = 0
                    speech_dispatched = 0
                    dialogue_dispatched = 0
                    dispatch_errs = 0
                    for _off, event in events:
                        try:
                            self.call_from_thread(
                                self._dispatch_event_for_agent, event, agent_name
                            )
                            replay_count += 1
                            if is_speech_tui_event(event):
                                speech_dispatched += 1
                            if is_dialogue_speech(event):
                                dialogue_dispatched += 1
                        except Exception as de:
                            dispatch_errs += 1
                            self._debug(f"REPLAY_DISPATCH_ERR {de!r}")
                    if dispatch_errs:
                        self._debug(f"REPLAY dispatch_errs={dispatch_errs}")
                    self._debug(
                        f"REPLAY kind={hist_kind} dispatched={replay_count} "
                        f"dialogue={dialogue_dispatched} speech={speech_dispatched} "
                        f"raw_lines={raw_line_n} want={want}"
                    )
                    t_label = f"-t{want}" if want else "tip"
                    n_tools = max(0, replay_count - dialogue_dispatched)
                    msg = (
                        f"Replay ({t_label}): {dialogue_dispatched} dialogue"
                        f", {n_tools} recent tools"
                        f" from {hist_kind}"
                    )
                    self.call_from_thread(lambda m=msg: self.notify(m, severity="information"))
                    time.sleep(1)
                    self.call_from_thread(self._force_scroll_bottom)
            except Exception as e:
                self._debug(f"REPLAY error: {e!r}")
                try:
                    err = f"Replay error: {e}"
                    self.call_from_thread(
                        lambda m=err: self.notify(m, severity="error", timeout=6)
                    )
                except Exception:
                    pass
            state["replay_done"] = True
            self._replay_done = True

            # Reload-safe delay chrome. If speech came *after* the delay in
            # hot (normal: delay tool then final agent text), only update the
            # header — do NOT append [aa.control] at the scroll bottom or the
            # tip looks like delay happened after the turn finished.
            try:
                info = self._scan_updates_for_latest_aa_control(updates_path)
                if info:
                    content = info.get("content") if isinstance(info, dict) else info
                    speech_after = (
                        bool(info.get("speech_after"))
                        if isinstance(info, dict)
                        else False
                    )
                    if content and speech_after:
                        self.call_from_thread(self._apply_delay_header, content)
                    elif content:
                        self.call_from_thread(
                            self._mount_aa_control_from_catchup, content
                        )
            except Exception as _e:
                self._debug(f"aa_control catch-up mount: {_e}")

        while not worker.is_cancelled:
            # Exit if tab was closed
            if agent_name not in self._agents or self._agent_state.get(agent_name, {}).get("removed"):
                return
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

                    parsed = []
                    skip_count = 0
                    hist_kind = state.get("history_kind") or kind
                    line_bridge = None
                    try:
                        core = str(Path(__file__).resolve().parent.parent / "core")
                        if core not in sys.path:
                            sys.path.insert(0, core)
                        from tui_history import line_to_tui_event as line_bridge
                    except Exception:
                        line_bridge = None
                    for line in lines:
                        if not line.strip():
                            continue
                        if len(line) > 2 * 1024 * 1024:
                            skip_count += 1
                            continue
                        if hist_kind == "hot" and line_bridge is not None:
                            ev = line_bridge(line, "hot")
                            if ev is not None:
                                parsed.append(ev)
                            else:
                                skip_count += 1
                        else:
                            try:
                                parsed.append(json.loads(line))
                            except json.JSONDecodeError:
                                skip_count += 1
                    batch = coalesce_events(parsed) if hist_kind != "hot" else parsed
                    for event in batch:
                        self.call_from_thread(
                            self._dispatch_event_for_agent, event, agent_name
                        )
                    self._debug(
                        f"TAIL_POLL kind={hist_kind} read={len(new_data)} lines={len(lines)} "
                        f"parsed={len(parsed)} coalesced={len(batch)} "
                        f"skipped={skip_count} offset={state['updates_offset']}"
                    )
                elif current_size < offset:
                    state["updates_offset"] = 0

            except FileNotFoundError:
                new_kind, new_path = self._resolve_display_history(agent_name)
                if new_path and Path(new_path).exists():
                    updates_path = Path(new_path)
                    state["history_kind"] = new_kind
                    state["updates_path"] = updates_path
                    state["updates_offset"] = 0
            except Exception:
                pass

            # Wake: inotify on hot.jsonl (shared hub, 1 fd / process) with timeout
            # safety net. Inactive tabs use longer timeout so they burn less CPU.
            try:
                from hot_inotify import wait_hot_change
                active = getattr(self, "_active_agent", None)
                inactive = active is not None and agent_name != active
                timeout = 2.0 if inactive else 1.0
                path = state.get("updates_path")
                if path and Path(path).exists():
                    wait_hot_change(path, timeout_s=timeout)
                else:
                    time.sleep(timeout)
            except Exception:
                time.sleep(0.5)

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
            tail_n = self._initial_tail_speech_count(agent_name)
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
            if agent_name not in self._agents or self._agent_state.get(agent_name, {}).get("removed"):
                return
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

    @staticmethod
    def _extract_interjections(text: str) -> tuple[str, list[str]]:
        """Extract <interjection> blocks — pure logic in chat_model."""
        return _cm_extract_interjections(text)

    @staticmethod
    def _update_text(update: dict) -> str:
        """Safe content.text from a session update (content may be dict/str/list)."""
        try:
            core = str(Path(__file__).resolve().parent.parent / "core")
            if core not in sys.path:
                sys.path.insert(0, core)
            from tui_history import tui_content_text
            return tui_content_text(update if isinstance(update, dict) else {})
        except Exception:
            c = (update or {}).get("content") if isinstance(update, dict) else None
            if isinstance(c, dict):
                return str(c.get("text") or "")
            if isinstance(c, str):
                return c
            return ""

    def _dispatch_event(self, event: dict) -> None:
        """Dispatch an updates.jsonl event to the appropriate renderer."""
        if not isinstance(event, dict):
            return
        params = event.get("params")
        if not isinstance(params, dict):
            params = {}
        update = params.get("update")
        if not isinstance(update, dict):
            update = {}
        event_type = update.get("sessionUpdate", "") or ""
        # Stash event timestamp for turn separators
        self._last_event_ts = event.get("timestamp")

        # Dual-path: pure ChatState always updated (testable; future UI driver)
        try:
            agent = self._active_agent
            st = self._agent_state.get(agent)
            if st is not None:
                cs = st.get("chat_state")
                if cs is None:
                    cs = ChatState()
                    st["chat_state"] = cs
                apply_event(cs, event)
                prune_items(cs)
                # Keep logical_turn in sync for header
                if cs.logical_turn:
                    st["logical_turn"] = cs.logical_turn
        except Exception:
            pass

        try:
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
        except Exception as e:
            # Never let one bad event abort replay / live tail
            self._debug(f"DISPATCH_ERR type={event_type!r} err={e!r}")
            try:
                self.notify(
                    f"Event error ({event_type or '?'}): {e}",
                    severity="warning",
                    timeout=4,
                )
            except Exception:
                pass

        # Bound DOM growth for long-lived sessions (7d+ must stay responsive).
        self._maybe_prune_after_mount()

    def _on_agent_message_chunk(self, update: dict) -> None:
        """Handle streaming agent message text."""
        text = self._update_text(update)
        if not text:
            return

        content = self._content_scroll()

        was_none = self._current_agent_msg is None
        if was_none:
            self._current_agent_msg = AgentMessage()
            content.mount(self._current_agent_msg)
            if self._following_tail():
                content.refresh(layout=True)

        self._current_agent_msg.append_chunk(text)

        # Check for complete ephact tags — collect to viewer but leave in chat
        full_text = self._current_agent_msg.full_text
        _, ephacts = extract_ephacts(full_text)
        # Track pushed count to avoid duplicates on subsequent chunks
        already_pushed = getattr(self._current_agent_msg, '_ephact_count', 0)
        new_ephacts = ephacts[already_pushed:]
        if new_ephacts:
            self._current_agent_msg._ephact_count = len(ephacts)
            try:
                viewer = self.query_one("#ephact-viewer", EphactViewer)
                for eph in new_ephacts:
                    viewer.push(self._active_agent, eph)
                    entry = EphactEntry(data=eph, agent=self._active_agent)
                    archive_ephact(
                        self._active_agent,
                        entry,
                        agent_home=Config.agent_home(self._active_agent),
                    )
                    self._debug(f"EPHACT type={eph.type} title={eph.title} agent={self._active_agent}")
            except NoMatches:
                pass

        self._debug(f"MSG_CHUNK len={len(text)} total={len(self._current_agent_msg._text)} new_widget={was_none}")
        self._scroll_to_bottom()

    def _on_agent_thought_chunk(self, update: dict) -> None:
        """Handle thinking/reasoning chunks."""
        text = self._update_text(update)
        if not text:
            return

        content = self._content_scroll()

        if self._current_thinking is None:
            self._current_thinking = ThinkingBlock()
            self._current_thinking.display = self._show_thinking
            content.mount(self._current_thinking)
            if self._following_tail():
                content.refresh(layout=True)

        self._current_thinking.append_chunk(text)
        if self._show_thinking:
            self._scroll_to_bottom()



    def _tool_command_from_update(self, update: dict) -> str:
        """Sticky command line for tool panels.

        Prefer a faithful summary of what ran, not a single "interesting" line
        that can disagree with stdout (e.g. multiline cd+sed+rg where scorer
        picked rg but body is sed dump).

        Order:
          1) full command string, compacted (keep multiple steps)
          2) Execute `…` title body
          3) description as last resort
        """
        command = None
        description = None

        ri = update.get("rawInput")
        if isinstance(ri, dict):
            for k in ("command", "cmd"):
                v = ri.get(k)
                if isinstance(v, str) and v.strip():
                    command = v.strip()
                    break
            d = ri.get("description")
            if isinstance(d, str) and d.strip():
                description = d.strip()
        elif isinstance(ri, str) and ri.strip():
            command = ri.strip()

        if command is None:
            for k in ("command", "arguments", "args", "input"):
                v = update.get(k)
                if isinstance(v, str) and v.strip():
                    command = v.strip()
                    break
                if isinstance(v, dict):
                    c = v.get("command") or v.get("cmd")
                    if isinstance(c, str) and c.strip():
                        command = c.strip()
                        break
                    d = v.get("description")
                    if isinstance(d, str) and d.strip() and not description:
                        description = d.strip()

        title = (update.get("title") or "").strip()
        if command is None and title.lower().startswith("execute `"):
            rest = title[9:]
            # may be multiline until closing `
            if "`" in rest:
                command = rest[: rest.rfind("`")].strip()
            else:
                command = rest.strip()

        if command:
            return self._compact_shell_command(command)
        if description:
            return description[:240]
        return ""

    @staticmethod
    def _compact_shell_command(command: str, max_chars: int = 240) -> str:
        """Collapse multiline script to one sticky line without dropping steps."""
        parts = []
        for ln in command.splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            parts.append(s)
        if not parts:
            return command.strip()[:max_chars]
        # Join steps so cd+sed+rg all visible (truncated as a whole)
        joined = " && ".join(parts)
        if len(joined) <= max_chars:
            return joined
        # Keep start (context) and end (often the real work)
        keep_head = max_chars // 2 - 2
        keep_tail = max_chars - keep_head - 5
        return joined[:keep_head].rstrip() + " … " + joined[-keep_tail:].lstrip()

    def _tool_update_blob(self, update: dict) -> str:
        """Flatten tool_call / tool_call_update fields for delay detection."""
        parts = [str(update.get("title") or "")]
        ri = update.get("rawInput")
        if isinstance(ri, dict):
            for k, v in ri.items():
                parts.append(v if isinstance(v, str) else str(v))
        elif isinstance(ri, str):
            parts.append(ri)
        for k in ("command", "arguments", "args", "input", "content", "rawInput"):
            v = update.get(k)
            if v is None or k == "rawInput":
                continue
            parts.append(v if isinstance(v, str) else str(v))
        return "\n".join(parts)

    def _scan_updates_for_latest_aa_control(self, updates_path, max_bytes: int = 2_000_000):
        """Catch-up: most recent delay tool_call in updates.jsonl or hot.jsonl.

        Returns dict ``{content, speech_after}`` or None.
        ``speech_after``: real dialogue exists *after* that delay in the scanned
        tail. When true, catch-up must NOT re-mount the control line at the
        bottom of the scroll (that made delay look like it happened *after*
        the agent finished speaking — Squiggy 09:52 delay then text; Eric).
        Header chrome can still show the delay.
        """
        try:
            path = Path(updates_path)
            if not path.exists():
                return None
            size = path.stat().st_size
            with open(path, "rb") as f:
                f.seek(max(0, size - max_bytes))
                if f.tell() > 0:
                    f.readline()
                data = f.read().decode("utf-8", errors="replace")
            lines = data.splitlines()
            latest = None
            latest_i = -1
            for i, line in enumerate(lines):
                if "delay" not in line or (
                    "tool_call" not in line and "tool_use" not in line
                ):
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ctrl = None
                update = (event.get("params") or {}).get("update") or {}
                et = update.get("sessionUpdate", "")
                if et in ("tool_call", "tool_call_update"):
                    ctrl = self._delay_control_from_tool_blob(
                        self._tool_update_blob(update)
                    )
                else:
                    body = event.get("body") or {}
                    if body.get("kind") in ("tool_call", "tool_result"):
                        blob = "\n".join(
                            str(x)
                            for x in (
                                body.get("name"),
                                body.get("args"),
                                body.get("input"),
                                body.get("content"),
                                body.get("text"),
                            )
                            if x is not None
                        )
                        ctrl = self._delay_control_from_tool_blob(blob)
                if ctrl:
                    latest = ctrl
                    latest_i = i
            if not latest:
                return None
            speech_after = False
            for line in lines[latest_i + 1 :]:
                if self._line_looks_like_dialogue_speech(line):
                    speech_after = True
                    break
            return {"content": latest, "speech_after": speech_after}
        except Exception as e:
            self._debug(f"aa_control catch-up scan failed: {e}")
            return None

    @staticmethod
    def _line_looks_like_dialogue_speech(line: str) -> bool:
        """Cheap hot/updates line check: non-chrome user/agent text."""
        if not line or "delay" in line[:80] and "tool" in line[:120]:
            pass
        try:
            core = str(Path(__file__).resolve().parent.parent / "core")
            import sys as _sys
            if core not in _sys.path:
                _sys.path.insert(0, core)
            from tui_history import (
                line_to_tui_event,
                is_dialogue_speech,
                cheap_hot_line_speech,
                is_chrome_speech,
            )
        except Exception:
            return False
        # Prefer full path when possible
        try:
            ev = line_to_tui_event(line, "hot")
            if ev is not None and is_dialogue_speech(ev):
                return True
        except Exception:
            pass
        try:
            hit = cheap_hot_line_speech(line)
            if hit is None:
                return False
            return not is_chrome_speech(hit[1])
        except Exception:
            return False

    def _apply_delay_header(self, content: str) -> None:
        """Header delay chrome only — no chat line."""
        try:
            import re
            header = self.query_one(AgentHeader)
            sm = re.search(r"delay:\s*([0-9.]+)s", content or "")
            if sm:
                header.delay_pattern = f"d:{sm.group(1)}s"
            elif content and "until_event" in content:
                header.delay_pattern = "d:until_event"
        except Exception as e:
            self._debug(f"aa_control header update failed: {e}")

    def _mount_aa_control_line(self, content: str) -> None:
        """Show agent AA control (delay) in the chat scroll."""
        try:
            content_scroll = self._content_scroll()
            ts_str = self._event_ts_str() if hasattr(self, "_event_ts_str") else time.strftime("%H:%M")
            state = self._agent_state.get(self._active_agent) or {}
            turn_num = int(state.get("logical_turn") or 0)
            trigger = classify_turn_trigger(content or "")
            content_scroll.mount(TurnSeparator(turn_num, trigger, ts_str))
            content_scroll.mount(SystemAlert(content, severity="info"))
            if self._following_tail():
                content_scroll.refresh(layout=True)
            self._scroll_to_bottom()
            self._debug(f"AA_CONTROL mounted: {(content or '')[:80]}")
        except Exception as e:
            self._debug(f"aa_control mount failed: {e}")

    def _mount_aa_control_from_catchup(self, content: str) -> None:
        """Mount control after history catch-up; refresh header delay chrome."""
        self._mount_aa_control_line(content)
        self._apply_delay_header(content)

    
    def _delay_control_from_tool_blob(self, text: str):
        """Derive [aa.control] delay line from tool_call payload text."""
        import re
        if not text or "delay" not in text:
            return None
        if not re.search(
            r"commands/cmd_[^\s\"']*\.json|commands/cmd_\$\{?date|commands/cmd_\$\(",
            text,
        ):
            return None
        if not re.search(
            r'["\']action["\']\s*:\s*["\']delay["\']|"action"\s*:\s*"delay"',
            text,
        ):
            return None
        sec = None
        m = re.search(
            r'["\']?seconds["\']?\s*:\s*["\']?(until_event|\d+(?:\.\d+)?)["\']?',
            text,
        )
        if m:
            sec = m.group(1)
        txt = None
        tm = re.search(r'["\']text["\']\s*:\s*["\']([^"\']{1,120})["\']', text)
        if tm:
            txt = tm.group(1)
        if sec == "until_event":
            detail = "until_event (standing by)"
        elif sec is not None:
            detail = f"{sec}s before next continue"
        else:
            detail = "delay registered"
        if txt:
            detail += f" — {txt}"
        return f"[aa.control] delay: {detail}"

    def _on_tool_call(self, update: dict) -> None:
        """Handle new tool call announcement (or refresh existing panel).

        Grok emits multiple tool_call frames per id (start + pending update).
        Always mounting a new panel left empty title-only husks on screen while
        completions updated a *second* panel.
        """
        tool_id = update.get("toolCallId", "") or ""
        title = update.get("title", "unknown tool")
        kind = update.get("kind", "") or ""

        self._current_agent_msg = None
        self._current_thinking = None

        content = self._content_scroll()
        existing = self._tool_panels.get(tool_id) if tool_id else None
        if existing is not None:
            if title and title not in ("unknown tool", "tool"):
                existing.tool_title = title
            if kind:
                existing.tool_kind = kind
            st = update.get("status")
            if st and st not in ("update", "started", "pending"):
                existing.set_status(st)
            cmd = self._tool_command_from_update(update)
            if cmd:
                existing.set_command(cmd)
            existing.refresh(layout=True)
            panel = existing
        else:
            panel = ToolCallPanel(tool_id, title, kind, ts=self._event_ts_str())
            if tool_id:
                self._tool_panels[tool_id] = panel
            content.mount(panel)
            cmd = self._tool_command_from_update(update)
            if cmd:
                panel.set_command(cmd)
            else:
                seed = self._tool_update_blob(update)
                if seed and seed.strip():
                    panel.set_command(seed.strip().splitlines()[0][:240])

        try:
            ctrl = self._delay_control_from_tool_blob(self._tool_update_blob(update))
            if ctrl:
                self._mount_aa_control_line(ctrl)
        except Exception as e:
            self._debug(f"delay-control detect failed: {e}")
        if self._following_tail():
            content.refresh(layout=True)
        self._debug(
            f"TOOL_CALL id={(tool_id or '')[:12]} title={str(title)[:40]} "
            f"reuse={existing is not None}"
        )
        self._scroll_to_bottom()

    def _on_tool_call_update(self, update: dict) -> None:
        """Handle tool call status/output updates."""
        tool_id = update.get("toolCallId", "") or ""
        status = update.get("status", "")
        kind = update.get("kind", "")
        title = update.get("title", "") or ""
        content_list = update.get("content", [])

        # Pure stdin/hot interjection carriers (tui_history body.kind=interjection)
        # must NOT leave an empty ToolCallPanel husk under the 🔔 panel.
        pure_interjection = bool(
            update.get("_aa_interjection")
            or title.lower() == "interjection"
            or str(tool_id).startswith("ij-")
        )

        def _gather_texts() -> list:
            texts = []
            if isinstance(content_list, list):
                for item in content_list:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "content":
                        inner = item.get("content", {})
                        if isinstance(inner, dict):
                            tt = inner.get("text", "")
                            if tt:
                                texts.append(str(tt))
                        elif isinstance(inner, str) and inner:
                            texts.append(inner)
            elif isinstance(content_list, str) and content_list.strip():
                texts.append(content_list)
            raw_out = update.get("rawOutput")
            if isinstance(raw_out, str) and raw_out.strip():
                texts.append(raw_out)
            elif raw_out is not None and not isinstance(raw_out, str):
                try:
                    core = str(Path(__file__).resolve().parent.parent / "core")
                    if core not in sys.path:
                        sys.path.insert(0, core)
                    from tui_history import _decode_tool_output
                    decoded = _decode_tool_output(raw_out)
                    if decoded:
                        texts.append(decoded)
                except Exception:
                    texts.append(str(raw_out))
            # dedup
            seen, ordered = set(), []
            for tt in texts:
                if tt in seen:
                    continue
                seen.add(tt)
                ordered.append(tt)
            return ordered

        ordered = _gather_texts()

        if pure_interjection:
            content = self._content_scroll()
            mounted_any = False
            for text in ordered:
                _clean, interjections = self._extract_interjections(text)
                for msg in interjections:
                    key = interjection_key(msg)
                    if key in self._seen_interjections:
                        continue
                    self._seen_interjections.add(key)
                    content.mount(InterjectionBlock(msg))
                    mounted_any = True
            if mounted_any and self._following_tail():
                content.refresh(layout=True)
            self._scroll_to_bottom()
            return

        panel = self._tool_panels.get(tool_id)

        if panel is None:
            # Tool call announcement might have been missed (e.g., started before TUI)
            display_title = title or f"tool {tool_id[:8]}"
            panel = ToolCallPanel(tool_id, display_title, kind, ts=self._event_ts_str())
            self._tool_panels[tool_id] = panel
            content = self._content_scroll()
            content.mount(panel)
            content.refresh(layout=True)

        if kind:
            panel.tool_kind = kind
        if title:
            panel.tool_title = title
        if status:
            panel.set_status(status)

        cmd = self._tool_command_from_update(update)
        if cmd:
            panel.set_command(cmd)

        for text in ordered:
            clean_text, interjections = self._extract_interjections(text)
            if interjections:
                content = self._content_scroll()
                mounted_any = False
                for msg in interjections:
                    key = interjection_key(msg)
                    if key in self._seen_interjections or msg in panel._mounted_interjections:
                        continue
                    self._seen_interjections.add(key)
                    panel._mounted_interjections.add(msg)
                    content.mount(InterjectionBlock(msg), before=panel)
                    mounted_any = True
                if mounted_any and self._following_tail():
                    content.refresh(layout=True)
            if clean_text.strip():
                panel.set_output(clean_text)
            elif interjections and not (panel.tool_output or "").strip():
                # Real tool whose stdout was only interjection chrome (BASH_ENV):
                # drop the empty husk if we never had real tool output.
                try:
                    if panel.tool_status in ("completed", "failed", "done", ""):
                        # keep panel for real tools with titles that aren't interjection
                        pass
                except Exception:
                    pass

        out_len = len(panel.tool_output) if panel.tool_output else 0
        self._debug(
            f"TOOL_UPDATE id={tool_id[:8]} status={status} out_len={out_len} "
            f"collapsed={panel._collapsed}"
        )
        self._scroll_to_bottom()

    def _on_plan(self, update: dict) -> None:
        """Handle plan/todo updates."""
        entries = update.get("entries", [])
        if not isinstance(entries, list) or not entries:
            return
        # PlanPanel does entry.get — coerce string entries
        entries = [
            (e if isinstance(e, dict) else {"content": str(e), "status": "pending"})
            for e in entries
        ]

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
        text = self._update_text(update)
        if not text:
            return

        # Skip messages we just sent from the TUI input bar (avoid double-display)
        if hasattr(self, "_last_sent_text") and self._last_sent_text and text.strip() == self._last_sent_text.strip():
            self._last_sent_text = None
            return

        # Harness <system-reminder> blobs: not operator turns — tool-like panel
        if is_system_reminder(text):
            self._current_agent_msg = None
            self._current_thinking = None
            content = self._content_scroll()
            content.mount(SystemReminderPanel(text))
            if self._following_tail():
                content.refresh(layout=True)
            self._scroll_to_bottom()
            return

        # Increment logical turn counter and insert separator
        state = self._agent_state[self._active_agent]
        state["logical_turn"] += 1
        turn_num = state["logical_turn"]
        trigger = classify_turn_trigger(text)

        ts_str = ""
        meta = update.get("_meta", {})
        if meta.get("ts"):
            ts_str = meta["ts"]
        elif hasattr(self, "_last_event_ts") and self._last_event_ts:
            ts_str = datetime.datetime.fromtimestamp(
                self._last_event_ts
            ).strftime("%a %b %d %H:%M:%S")

        self._current_agent_msg = None
        self._current_thinking = None

        content = self._content_scroll()
        content.mount(TurnSeparator(turn_num, trigger, ts_str))
        content.mount(UserMessage(text))
        content.refresh(layout=True)
        self._scroll_to_bottom()

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
        if not isinstance(snapshot, dict):
            snapshot = {}
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
        """Lazy-load older history (scroll-up / PageUp).

        I/O + parse run on a **worker thread** so the UI stays responsive.
        Widget mount stays on the main thread (Textual requirement).
        This is the path that froze when multi-tab Astro scrolled the dense
        evening band — bulk -t N replay already used a worker.
        """
        agent_name = agent_name or self._active_agent
        state = self._agent_state[agent_name]

        if state["loading_history"] or state["earliest_offset"] <= 0:
            return
        updates_path = state.get("updates_path")
        if not updates_path:
            return

        state["loading_history"] = True
        try:
            content = self._content_scroll(agent_name)
            first_child = content.children[0] if content.children else None
        except Exception:
            state["loading_history"] = False
            return

        # Capture scroll anchor + offsets for the worker (no UI objects in thread)
        earliest = int(state["earliest_offset"])
        hist_kind = state.get("history_kind") or "updates"
        path_str = str(updates_path)

        def _work(
            an=agent_name,
            path=path_str,
            earliest_off=earliest,
            kind=hist_kind,
            anchor=first_child,
        ):
            try:
                payload = self._scan_older_history_events(
                    path, earliest_off, kind
                )
                self.call_from_thread(
                    self._apply_older_history_mount,
                    an,
                    payload,
                    anchor,
                )
            except Exception as e:
                def _fail(err=e, name=an):
                    try:
                        self.notify(f"History error: {err}", severity="error")
                    except Exception:
                        pass
                    st = self._agent_state.get(name) or {}
                    st["loading_history"] = False

                try:
                    self.call_from_thread(_fail)
                except Exception:
                    st = self._agent_state.get(an) or {}
                    st["loading_history"] = False

        self.run_worker(_work, thread=True, name=f"hist_load_{agent_name}")

    def _scan_older_history_events(
        self, path_str: str, earliest_offset: int, hist_kind: str
    ) -> dict:
        """Worker: walk hot/updates backward; return plain event dicts (no widgets).

        Delegates to tui_history.scan_older_history_events so fat lines longer
        than the read window cannot pin the cursor (infinite CPU spin).
        """
        try:
            core = str(Path(__file__).resolve().parent.parent / "core")
            if core not in sys.path:
                sys.path.insert(0, core)
            from tui_history import scan_older_history_events
        except Exception as e:
            return {
                "events": [],
                "new_earliest": max(0, int(earliest_offset)),
                "speech_n": 0,
                "bytes_read": 0,
                "error": str(e),
            }
        return scan_older_history_events(path_str, earliest_offset, hist_kind)

    def _apply_older_history_mount(
        self, agent_name: str, payload: dict, first_child
    ) -> None:
        """Main thread: build widgets from worker payload and prepend."""
        state = self._agent_state.get(agent_name)
        if not state:
            return
        try:
            content = self._content_scroll(agent_name)
            events = payload.get("events") or []
            state["earliest_offset"] = int(payload.get("new_earliest") or 0)
            widgets_to_prepend = []

            for event in events:
                try:
                    update = event.get("params", {}).get("update", {}) or {}
                    event_type = update.get("sessionUpdate", "")

                    if event_type == "agent_message_chunk":
                        text = self._update_text(update)
                        if text:
                            cleaned, ephacts = extract_ephacts(text)
                            if ephacts:
                                try:
                                    viewer = self.query_one("#ephact-viewer", EphactViewer)
                                    for eph in ephacts:
                                        viewer.push(agent_name, eph)
                                except NoMatches:
                                    pass
                            if len(text) > 8000:
                                text = text[:8000] + "\n… [truncated for TUI]"
                            msg = AgentMessage()
                            msg._text = text
                            msg._chunks = [text]
                            widgets_to_prepend.append(msg)
                    elif event_type == "user_message_chunk":
                        text = self._update_text(update)
                        if text:
                            if is_system_reminder(text):
                                widgets_to_prepend.append(SystemReminderPanel(text))
                            else:
                                widgets_to_prepend.append(UserMessage(text))
                    elif event_type in ("tool_call_update", "tool_call"):
                        title = update.get("title", "tool")
                        status = update.get(
                            "status",
                            "completed" if event_type == "tool_call_update" else "running",
                        )
                        kind = update.get("kind", "")
                        tool_id = update.get("toolCallId", "")
                        ts_raw = event.get("timestamp")
                        try:
                            ts_str = (
                                datetime.datetime.fromtimestamp(float(ts_raw)).strftime(
                                    "%H:%M:%S"
                                )
                                if ts_raw
                                else ""
                            )
                        except Exception:
                            ts_str = ""
                        panel = ToolCallPanel(tool_id, title, kind, ts=ts_str)
                        panel.tool_status = status
                        panel._collapsed = True
                        raw_parts = []
                        content_list = update.get("content") or []
                        if isinstance(content_list, list):
                            for item in content_list:
                                if not isinstance(item, dict):
                                    continue
                                if item.get("type") == "content":
                                    inner = item.get("content", {})
                                    if isinstance(inner, dict):
                                        raw_parts.append(inner.get("text") or "")
                                    elif isinstance(inner, str):
                                        raw_parts.append(inner)
                        raw_out = update.get("rawOutput")
                        if isinstance(raw_out, str) and raw_out:
                            raw_parts.append(raw_out)
                        raw = "\n".join(raw_parts)
                        clean, intj_msgs = self._extract_interjections(raw)
                        pure_ij = bool(
                            update.get("_aa_interjection")
                            or (title or "").lower() == "interjection"
                            or str(tool_id).startswith("ij-")
                        )
                        for msg in intj_msgs:
                            key = interjection_key(msg)
                            if key in self._seen_interjections:
                                continue
                            self._seen_interjections.add(key)
                            widgets_to_prepend.append(InterjectionBlock(msg))
                        if pure_ij and not clean.strip():
                            pass
                        else:
                            if clean.strip():
                                panel.tool_output = (
                                    clean
                                    if len(clean) <= 4000
                                    else clean[:4000] + "\n… [truncated]"
                                )
                            widgets_to_prepend.append(panel)
                    elif event_type == "hook_annotation":
                        message = update.get("message", "")
                        if message:
                            widgets_to_prepend.append(HookAnnotation(message))
                except Exception:
                    continue

            if widgets_to_prepend:
                content._follow_tail = False
                anchor = first_child
                try:
                    if first_child is not None:
                        content.mount(*widgets_to_prepend, before=first_child)
                    else:
                        for w in widgets_to_prepend:
                            content.mount(w)
                except Exception as e:
                    self.notify(f"Mount error: {e}", severity="error")
                    for w in widgets_to_prepend:
                        try:
                            content.mount(w)
                        except Exception:
                            pass

                def _restore_anchor(a=anchor) -> None:
                    try:
                        if a is not None and a.is_attached:
                            content.scroll_to_widget(a, animate=False, top=True)
                    except Exception:
                        pass

                self.call_after_refresh(_restore_anchor)
                self.set_timer(0.05, _restore_anchor)
                self.set_timer(0.2, _restore_anchor)

            if state["earliest_offset"] <= 0:
                self.notify("Reached beginning of session", severity="information")
        except Exception as e:
            self.notify(f"History error: {e}", severity="error")
        finally:
            state["loading_history"] = False

    _pending_scroll_timer = None

    _dispatching_agent = None


    def _event_ts_str(self) -> str:
        """HH:MM:SS from last event timestamp for panel citations."""
        ts = getattr(self, "_last_event_ts", None)
        if not ts:
            return time.strftime("%H:%M:%S")
        try:
            return datetime.datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
        except Exception:
            return time.strftime("%H:%M:%S")

    def _following_tail(self) -> bool:
        """True if the visible content scroll is pinned to the live tail."""
        try:
            if self._dispatching_agent and self._dispatching_agent != self._active_agent:
                return False  # background agent — don't thrash visible layout
            return bool(self._content_scroll()._follow_tail)
        except Exception:
            return True

    def _stream_refresh(self, widget) -> None:
        """Refresh a streaming widget; layout only when following tail."""
        if widget is None:
            return
        if self._following_tail():
            widget.refresh()
        else:
            # User reading history: update data only, minimal layout cost
            widget.refresh()


    def _prune_scrollback(self, agent: str | None = None) -> None:
        """Drop oldest chat widgets so long-lived sessions stay responsive.

        Textual's VerticalScroll lays out every child. After days of tool calls
        and speech, the DOM hits 1000+ widgets and input freezes. Keep a fixed
        window of recent widgets. Only runs while following the live tail so
        a reader parked in history is not yanked.
        """
        agent = agent or self._active_agent
        st = self._agent_state.get(agent)
        if st is None or st.get("removed"):
            return
        # cadence
        n = int(st.get("_prune_counter", 0)) + 1
        st["_prune_counter"] = n
        if n % _PRUNE_EVERY_N != 0:
            return
        try:
            content = self._content_scroll(agent)
        except NoMatches:
            return
        if not getattr(content, "_follow_tail", True):
            return
        children = list(content.children)
        excess = len(children) - MAX_SCROLLBACK_WIDGETS
        if excess <= 0:
            return
        protect = {
            id(st.get("current_agent_msg")),
            id(st.get("current_thinking")),
        }
        panels = st.get("tool_panels") or {}
        removed = 0
        for w in children:
            if removed >= excess:
                break
            if id(w) in protect:
                continue
            # Never drop the very last few (visible tail)
            if w is children[-1] or (len(children) > 1 and w is children[-2]):
                continue
            try:
                tid = getattr(w, "tool_id", None)
                if tid and tid in panels and panels.get(tid) is w:
                    panels.pop(tid, None)
                w.remove()
                removed += 1
            except Exception:
                pass
        if removed:
            self._debug(
                f"PRUNE agent={agent} removed={removed} left={len(children)-removed}"
            )

    def _maybe_prune_after_mount(self, agent: str | None = None) -> None:
        """Call after mounting chat widgets on the live path."""
        try:
            self._prune_scrollback(agent)
        except Exception:
            pass

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
        """Force scroll to bottom -- used after replay completes.

        Delayed follow-ups only run while still following the tail, so a user
        who scrolls up to read is not yanked back (or through) the timeline.
        """
        try:
            scroll = self._content_scroll()
            scroll._follow_tail = True
            scroll.scroll_end(animate=False)

            def _later() -> None:
                try:
                    s = self._content_scroll()
                    if s._follow_tail:
                        s.scroll_end(animate=False)
                except Exception:
                    pass

            self.set_timer(0.5, _later)
            self.set_timer(2.0, _later)
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
        "--agent", "-a", action="append", dest="agents", default=None,
        help="Agent to open (repeatable). Default: Trip only. Use tab [+] to add more.",
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
        help="Catch-up last N dialogue lines (+ collapsed tools in span). "
             "PageUp loads older. Default 50 primary / 25 secondary."
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
    parser.add_argument(
        "--light", action="store_true",
        help="Use Grok Day light theme (alias for --theme grokday)",
    )
    parser.add_argument(
        "--theme", default=None,
        help="Theme id or 'auto' (see tui/themes/). Overrides saved preference for this run.",
    )
    args = parser.parse_args()

    open_agents = args.agents if args.agents else ["Trip"]
    Config.AGENT_NAME = open_agents[0]
    Config.AGENTS_HOME = args.agents_home
    Config.set_env(TuiEnv.from_defaults(args.agents_home))
    Config.UPDATES_FILE = args.updates
    Config.GROK_SESSIONS_DIR = args.sessions_dir
    Config.API_URL = args.api_url
    if args.operator:
        Config.OPERATOR_NAME = args.operator
        # Don't save to disk — --operator is ephemeral for test instances

    if args.light:
        set_theme("grokday")
    elif args.theme:
        set_theme(args.theme)

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

    # Open only CLI-selected agents; [+] adds more from agents.json catalog.
    # Dedupe preserving order.
    seen = set()
    open_list = []
    for name in open_agents:
        if name not in seen:
            seen.add(name)
            open_list.append(name)
    catalog = set(Config.list_catalog_agents())
    # Allow CLI names even if catalog miss (still try to open)
    app = AsdaaasTUI(agents=open_list)

    if args.debug_log:
        AsdaaasTUI.enable_debug_log(args.debug_log)

    if args.replay or args.tail:
        app._replay_mode = True
    if args.tail:
        app._tail_count = args.tail

    app.run()


if __name__ == "__main__":
    main()
