#!/usr/bin/env python3
# tui_adapter.py — Textual-based TUI for asdaaas agent sessions
# Phase 1: Full-screen development interface matching grok TUI experience
# Routes through asdaaas adapter pattern (inbox/outbox + updates.jsonl tailing)
#
# Architecture:
#   Input:  User types → write to asdaaas TUI adapter inbox
#   Output: Tail updates.jsonl → render events in real-time
#   Status: Poll health.json/gaze.json for status bar
#
# Event types rendered:
#   agent_message_chunk  → Streaming markdown text
#   tool_call            → Tool panel header (bordered box)
#   tool_call_update     → Tool panel body (status, output, diffs)
#   plan                 → Todo list panel
#   hook_annotation      → Dimmed status line
#   user_message_chunk   → User message (right-aligned)
#   task_backgrounded    → Background task notification
#   task_completed       → Task completion notification
"""
tui_adapter.py -- asdaaas Terminal UI
======================================

A rich terminal interface for interacting with agents through asdaaas.
Replaces the grok TUI's direct connection with an asdaaas-routed experience.

Features:
  - Markdown rendering (headers, bold, italic, code blocks, lists)
  - Syntax-highlighted code blocks
  - Structured display: speech, thoughts, tool output
  - Persistent status bar: agent, context %, gaze, health
  - Scrollable history
  - Slash commands: /status, /gaze, /agents, /clear, /quit
  - Multi-agent support

Architecture:
  User types -> tui writes to agent's adapter inbox
  -> asdaaas pipes to agent stdin -> agent responds
  -> asdaaas writes to adapter outbox -> tui reads and renders

Usage:
  python3 tui_adapter.py --agent Trip
  python3 tui_adapter.py --agent Trip --agents-home ~/agents

Author: MikeyV-Trip, 2026-04-01
"""

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import time
import uuid
import threading
import signal
from pathlib import Path
from datetime import datetime


# ============================================================================
# CONFIGURATION
# ============================================================================

try:
    from asdaaas_config import config
except ModuleNotFoundError:
    import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'core'))
    from asdaaas_config import config as _asdaaas_config

class Config:
    """Mutable global configuration."""
    agents_home = _asdaaas_config.agents_home
    adapter_name = "tui"
    poll_interval = 0.3  # seconds between outbox polls
    health_poll_interval = 2.0  # seconds between health checks


# ============================================================================
# ANSI ESCAPE CODES
# ============================================================================

class Style:
    """ANSI escape code constants."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"
    STRIKETHROUGH = "\033[9m"

    # Foreground
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    GRAY = "\033[90m"

    # Bright foreground
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"

    # Background
    BG_BLACK = "\033[40m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"
    BG_MAGENTA = "\033[45m"
    BG_CYAN = "\033[46m"
    BG_WHITE = "\033[47m"
    BG_GRAY = "\033[100m"

    # 256-color (for subtle backgrounds)
    BG_DARK_GRAY = "\033[48;5;236m"
    BG_DARKER_GRAY = "\033[48;5;234m"
    FG_LIGHT_GRAY = "\033[38;5;250m"
    FG_DARK_GRAY = "\033[38;5;242m"
    FG_CODE_GREEN = "\033[38;5;114m"
    FG_KEYWORD = "\033[38;5;176m"
    FG_STRING = "\033[38;5;180m"
    FG_COMMENT = "\033[38;5;242m"
    FG_NUMBER = "\033[38;5;141m"

    # Cursor and screen
    CLEAR_LINE = "\033[2K"
    CURSOR_UP = "\033[1A"
    SAVE_CURSOR = "\033[s"
    RESTORE_CURSOR = "\033[u"
    HIDE_CURSOR = "\033[?25l"
    SHOW_CURSOR = "\033[?25h"


S = Style  # shorthand


# ============================================================================
# TERMINAL UTILITIES
# ============================================================================

def term_width():
    """Get terminal width, default 80."""
    return shutil.get_terminal_size((80, 24)).columns


def term_height():
    """Get terminal height, default 24."""
    return shutil.get_terminal_size((80, 24)).lines


def strip_ansi(text):
    """Remove ANSI escape codes from text for length calculations."""
    return re.sub(r'\033\[[0-9;]*[a-zA-Z]', '', text)


def visible_len(text):
    """Length of text as displayed (without ANSI codes)."""
    return len(strip_ansi(text))


# ============================================================================
# MARKDOWN RENDERER
# ============================================================================

class MarkdownRenderer:
    """Renders markdown text to ANSI-styled terminal output.

    Handles:
      - Headers (# ## ###)
      - Bold (**text**), italic (*text*), bold-italic (***text***)
      - Inline code (`code`)
      - Code blocks (```lang ... ```)
      - Bullet lists (- item, * item)
      - Numbered lists (1. item)
      - Blockquotes (> text)
      - Horizontal rules (---, ***)
      - Links [text](url)
    """

    def __init__(self, width=None):
        self.width = width or term_width()

    def render(self, text):
        """Render markdown text to ANSI string."""
        lines = text.split('\n')
        output = []
        i = 0
        while i < len(lines):
            line = lines[i]

            # Code block
            if line.strip().startswith('```'):
                lang = line.strip()[3:].strip()
                code_lines = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith('```'):
                    code_lines.append(lines[i])
                    i += 1
                i += 1  # skip closing ```
                output.append(self._render_code_block(code_lines, lang))
                continue

            # Header
            header_match = re.match(r'^(#{1,6})\s+(.+)$', line)
            if header_match:
                level = len(header_match.group(1))
                text_content = header_match.group(2)
                output.append(self._render_header(text_content, level))
                i += 1
                continue

            # Horizontal rule
            if re.match(r'^[\s]*[-*_]{3,}\s*$', line):
                output.append(self._render_hr())
                i += 1
                continue

            # Blockquote
            if line.strip().startswith('>'):
                quote_lines = []
                while i < len(lines) and lines[i].strip().startswith('>'):
                    quote_lines.append(re.sub(r'^>\s?', '', lines[i]))
                    i += 1
                output.append(self._render_blockquote(quote_lines))
                continue

            # Bullet list
            bullet_match = re.match(r'^(\s*)([-*+])\s+(.+)$', line)
            if bullet_match:
                indent = len(bullet_match.group(1))
                content = bullet_match.group(3)
                output.append(self._render_bullet(content, indent))
                i += 1
                continue

            # Numbered list
            num_match = re.match(r'^(\s*)(\d+)\.\s+(.+)$', line)
            if num_match:
                indent = len(num_match.group(1))
                num = num_match.group(2)
                content = num_match.group(3)
                output.append(self._render_numbered(content, num, indent))
                i += 1
                continue

            # Empty line
            if not line.strip():
                output.append('')
                i += 1
                continue

            # Regular paragraph
            output.append(self._render_inline(line))
            i += 1

        return '\n'.join(output)

    def _render_header(self, text, level):
        text = self._render_inline(text)
        if level == 1:
            bar = S.CYAN + S.BOLD + ('=' * min(self.width - 2, visible_len(text) + 4)) + S.RESET
            return f"\n{bar}\n{S.BOLD}{S.BRIGHT_CYAN}  {text}{S.RESET}\n{bar}"
        elif level == 2:
            bar = S.BLUE + ('-' * min(self.width - 2, visible_len(text) + 4)) + S.RESET
            return f"\n{S.BOLD}{S.BRIGHT_BLUE}  {text}{S.RESET}\n{bar}"
        elif level == 3:
            return f"\n{S.BOLD}{S.BLUE}  {text}{S.RESET}"
        else:
            return f"\n{S.BOLD}{text}{S.RESET}"

    def _render_code_block(self, lines, lang=''):
        """Render a fenced code block with background and optional language tag."""
        w = self.width - 4
        result = []
        if lang:
            result.append(f"  {S.DIM}{S.FG_DARK_GRAY}{lang}{S.RESET}")
        result.append(f"  {S.BG_DARKER_GRAY}{' ' * w}{S.RESET}")
        for line in lines:
            # Pad to fill background
            visible = line.expandtabs(4)
            pad = max(0, w - len(visible))
            styled = self._syntax_highlight(visible, lang) if lang else f"{S.FG_LIGHT_GRAY}{visible}"
            result.append(f"  {S.BG_DARKER_GRAY}{styled}{' ' * pad}{S.RESET}")
        result.append(f"  {S.BG_DARKER_GRAY}{' ' * w}{S.RESET}")
        return '\n'.join(result)

    def _syntax_highlight(self, line, lang):
        """Basic syntax highlighting for common languages."""
        # Keywords for python/js/rust/bash
        keywords_py = {'def', 'class', 'import', 'from', 'return', 'if', 'elif',
                       'else', 'for', 'while', 'try', 'except', 'with', 'as',
                       'yield', 'async', 'await', 'raise', 'pass', 'break',
                       'continue', 'in', 'not', 'and', 'or', 'is', 'None',
                       'True', 'False', 'lambda', 'self'}
        keywords_js = {'function', 'const', 'let', 'var', 'return', 'if', 'else',
                       'for', 'while', 'try', 'catch', 'throw', 'new', 'class',
                       'import', 'export', 'from', 'async', 'await', 'yield',
                       'true', 'false', 'null', 'undefined', 'this', 'typeof'}
        keywords_rs = {'fn', 'let', 'mut', 'pub', 'struct', 'enum', 'impl',
                       'trait', 'use', 'mod', 'crate', 'self', 'super', 'match',
                       'if', 'else', 'for', 'while', 'loop', 'return', 'async',
                       'await', 'where', 'type', 'const', 'static', 'move',
                       'true', 'false', 'Some', 'None', 'Ok', 'Err'}
        keywords_sh = {'if', 'then', 'else', 'elif', 'fi', 'for', 'do', 'done',
                       'while', 'case', 'esac', 'function', 'return', 'exit',
                       'echo', 'export', 'source', 'local', 'readonly', 'set'}

        kw_map = {
            'python': keywords_py, 'py': keywords_py,
            'javascript': keywords_js, 'js': keywords_js, 'typescript': keywords_js, 'ts': keywords_js,
            'rust': keywords_rs, 'rs': keywords_rs,
            'bash': keywords_sh, 'sh': keywords_sh, 'shell': keywords_sh, 'zsh': keywords_sh,
        }
        keywords = kw_map.get(lang.lower(), set())

        # Comment detection
        stripped = line.lstrip()
        if stripped.startswith('#') or stripped.startswith('//'):
            return f"{S.FG_COMMENT}{line}"

        # Token-by-token highlighting
        result = []
        # Split preserving whitespace and punctuation
        tokens = re.findall(r'(\s+|"[^"]*"|\'[^\']*\'|\b\w+\b|[^\s\w])', line)
        for tok in tokens:
            if tok.startswith('"') or tok.startswith("'"):
                result.append(f"{S.FG_STRING}{tok}")
            elif tok in keywords:
                result.append(f"{S.FG_KEYWORD}{tok}")
            elif re.match(r'^\d+\.?\d*$', tok):
                result.append(f"{S.FG_NUMBER}{tok}")
            elif tok.isspace():
                result.append(tok)
            else:
                result.append(f"{S.FG_LIGHT_GRAY}{tok}")
        return ''.join(result)

    def _render_inline(self, text):
        """Render inline markdown: bold, italic, code, links."""
        # Links: [text](url)
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)',
                       lambda m: f"{S.UNDERLINE}{S.CYAN}{m.group(1)}{S.RESET} {S.DIM}({m.group(2)}){S.RESET}",
                       text)
        # Bold-italic: ***text***
        text = re.sub(r'\*\*\*(.+?)\*\*\*',
                       lambda m: f"{S.BOLD}{S.ITALIC}{m.group(1)}{S.RESET}", text)
        # Bold: **text**
        text = re.sub(r'\*\*(.+?)\*\*',
                       lambda m: f"{S.BOLD}{m.group(1)}{S.RESET}", text)
        # Italic: *text* (but not inside **)
        text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)',
                       lambda m: f"{S.ITALIC}{m.group(1)}{S.RESET}", text)
        # Strikethrough: ~~text~~
        text = re.sub(r'~~(.+?)~~',
                       lambda m: f"{S.STRIKETHROUGH}{m.group(1)}{S.RESET}", text)
        # Inline code: `code`
        text = re.sub(r'`([^`]+)`',
                       lambda m: f"{S.BG_DARK_GRAY}{S.FG_CODE_GREEN} {m.group(1)} {S.RESET}", text)
        return text

    def _render_blockquote(self, lines):
        rendered = [self._render_inline(l) for l in lines]
        bar = f"{S.DIM}{S.CYAN}"
        result = [f"  {bar}|{S.RESET} {S.DIM}{l}{S.RESET}" for l in rendered]
        return '\n'.join(result)

    def _render_bullet(self, text, indent=0):
        pad = '  ' * (indent // 2)
        bullet = f"{S.CYAN}\u2022{S.RESET}"
        return f"  {pad}{bullet} {self._render_inline(text)}"

    def _render_numbered(self, text, num, indent=0):
        pad = '  ' * (indent // 2)
        return f"  {pad}{S.CYAN}{num}.{S.RESET} {self._render_inline(text)}"

    def _render_hr(self):
        w = min(self.width - 4, 60)
        return f"  {S.DIM}{'─' * w}{S.RESET}"


# ============================================================================
# ADAPTER FILESYSTEM INTERFACE
# ============================================================================

def agent_adapter_dir(agent_name, adapter_name=None):
    """Get the adapter directory for an agent."""
    if adapter_name is None:
        adapter_name = Config.adapter_name
    return Config.agents_home / agent_name / "asdaaas" / "adapters" / adapter_name


def agent_asdaaas_dir(agent_name):
    """Get the asdaaas directory for an agent."""
    return Config.agents_home / agent_name / "asdaaas"


def ensure_dirs(agent_name):
    """Create inbox/outbox directories for this adapter."""
    base = agent_adapter_dir(agent_name)
    (base / "inbox").mkdir(parents=True, exist_ok=True)
    (base / "outbox").mkdir(parents=True, exist_ok=True)


def write_message(agent_name, text, sender="eric"):
    """Write a message to the agent's TUI adapter inbox."""
    inbox = agent_adapter_dir(agent_name) / "inbox"
    msg_id = str(uuid.uuid4())

    msg = {
        "id": msg_id,
        "from": sender,
        "to": agent_name,
        "text": text,
        "adapter": Config.adapter_name,
        "room": "tui",
        "meta": {"source": "tui"},
        "ts": time.time(),
    }

    # Atomic write: mkstemp + rename
    fd, tmp_path = tempfile.mkstemp(dir=str(inbox), suffix=".tmp", prefix="msg_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(msg, f)
        final_path = tmp_path.replace(".tmp", ".json")
        os.rename(tmp_path, final_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return msg_id


def poll_outbox(agent_name):
    """Read and delete responses from the adapter outbox. Returns list of dicts."""
    outbox = agent_adapter_dir(agent_name) / "outbox"
    if not outbox.exists():
        return []

    responses = []
    for entry in sorted(outbox.iterdir()):
        if not entry.name.endswith(".json"):
            continue
        try:
            with open(entry) as f:
                data = json.load(f)
            responses.append(data)
            entry.unlink()
        except (json.JSONDecodeError, OSError):
            pass

    return responses


def read_health(agent_name):
    """Read agent health.json. Returns dict or None."""
    health_file = agent_asdaaas_dir(agent_name) / "health.json"
    try:
        with open(health_file) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def read_gaze(agent_name):
    """Read agent gaze.json. Returns dict or None."""
    gaze_file = agent_asdaaas_dir(agent_name) / "gaze.json"
    try:
        with open(gaze_file) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


# ============================================================================
# STATUS BAR
# ============================================================================

class StatusBar:
    """Persistent status bar at the bottom of the terminal.

    Shows: agent name | context usage | gaze target | health status | time
    """

    def __init__(self, agent_name):
        self.agent_name = agent_name
        self.health = None
        self.gaze = None
        self.running = True
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)

    def start(self):
        # Initial sync read before starting background thread
        self.health = read_health(self.agent_name)
        self.gaze = read_gaze(self.agent_name)
        self._thread.start()

    def stop(self):
        self.running = False

    def _poll_loop(self):
        while self.running:
            try:
                with self._lock:
                    self.health = read_health(self.agent_name)
                    self.gaze = read_gaze(self.agent_name)
            except Exception:
                pass
            time.sleep(Config.health_poll_interval)

    def render(self):
        """Render the status bar as a single ANSI string."""
        w = term_width()

        # Agent name
        agent_str = f" {self.agent_name} "

        # Context usage
        with self._lock:
            health = self.health
            gaze = self.gaze

        if health:
            total = health.get("totalTokens", 0)
            window = health.get("contextWindow", 200000)
            pct = (total / window * 100) if window else 0
            status = health.get("status", "?")

            # Color-code context percentage
            if pct < 50:
                pct_color = S.BRIGHT_GREEN
            elif pct < 70:
                pct_color = S.BRIGHT_YELLOW
            elif pct < 85:
                pct_color = S.BRIGHT_RED
            else:
                pct_color = S.RED + S.BOLD

            ctx_str = f" {pct_color}{pct:.0f}%{S.RESET}{S.BG_GRAY}{S.WHITE} ctx "
            status_str = f" {status} "
        else:
            ctx_str = " ?? "
            status_str = " offline "

        # Gaze target
        if gaze:
            speech = gaze.get("speech", {})
            target = speech.get("target", "?")
            room = speech.get("params", {}).get("room", "")
            gaze_str = f" {target}/{room} " if room else f" {target} "
        else:
            gaze_str = " ? "

        # Time
        time_str = f" {datetime.now().strftime('%H:%M')} "

        # Compose bar
        bar = (f"{S.BG_GRAY}{S.BRIGHT_WHITE}{S.BOLD}{agent_str}{S.RESET}"
               f"{S.BG_GRAY}{S.WHITE}{S.DIM} | {S.RESET}"
               f"{S.BG_GRAY}{S.WHITE}{ctx_str}{S.RESET}"
               f"{S.BG_GRAY}{S.WHITE}{S.DIM} | {S.RESET}"
               f"{S.BG_GRAY}{S.WHITE} gaze:{gaze_str}{S.RESET}"
               f"{S.BG_GRAY}{S.WHITE}{S.DIM} | {S.RESET}"
               f"{S.BG_GRAY}{S.WHITE}{status_str}{S.RESET}")

        # Pad to full width
        vis_len = visible_len(bar)
        pad = max(0, w - vis_len)
        bar += f"{S.BG_GRAY}{' ' * pad}{S.RESET}"

        return bar

    def draw(self):
        """Draw the status bar at the current cursor position."""
        sys.stdout.write(self.render() + '\n')
        sys.stdout.flush()


# ============================================================================
# RESPONSE DISPLAY
# ============================================================================

class ResponseDisplay:
    """Formats and displays agent responses with rich rendering."""

    def __init__(self):
        self.md = MarkdownRenderer()
        self.history = []  # list of (timestamp, from, content_type, text)

    def show_response(self, resp):
        """Display a single outbox response."""
        text = resp.get("text", "")
        from_agent = resp.get("from", "agent")
        content_type = resp.get("content_type", "speech")
        ts = datetime.now().strftime("%H:%M:%S")

        self.history.append((ts, from_agent, content_type, text))

        if content_type == "thoughts":
            self._show_thoughts(text, from_agent, ts)
        else:
            self._show_speech(text, from_agent, ts)

    def _show_speech(self, text, from_agent, ts):
        """Render agent speech with markdown."""
        # Header line
        header = (f"\n{S.BOLD}{S.GREEN}"
                  f"{'─' * 3} {from_agent} {S.DIM}{ts}{S.RESET}"
                  f"{S.GREEN}{'─' * max(3, term_width() - len(from_agent) - len(ts) - 10)}"
                  f"{S.RESET}")
        sys.stdout.write(header + '\n')

        # Render markdown
        rendered = self.md.render(text)
        sys.stdout.write(rendered + '\n')

        # Footer
        sys.stdout.write(f"{S.GREEN}{'─' * min(term_width() - 2, 60)}{S.RESET}\n")
        sys.stdout.flush()

    def _show_thoughts(self, text, from_agent, ts):
        """Render agent thoughts (dimmed, indented)."""
        sys.stdout.write(f"\n{S.DIM}{S.MAGENTA}  thoughts ({ts}):{S.RESET}\n")
        for line in text.split('\n'):
            sys.stdout.write(f"{S.DIM}  {S.MAGENTA}| {line}{S.RESET}\n")
        sys.stdout.flush()

    def show_system(self, text):
        """Show a system message."""
        sys.stdout.write(f"{S.YELLOW}[system]{S.RESET} {text}\n")
        sys.stdout.flush()

    def show_error(self, text):
        """Show an error message."""
        sys.stdout.write(f"{S.RED}[error]{S.RESET} {text}\n")
        sys.stdout.flush()

    def show_user(self, text, sender="eric"):
        """Echo user message in a subtle way."""
        ts = datetime.now().strftime("%H:%M:%S")
        sys.stdout.write(f"{S.DIM}{S.BLUE}  {sender} ({ts}): {text}{S.RESET}\n")
        sys.stdout.flush()


# ============================================================================
# RESPONSE POLLER (background thread)
# ============================================================================

class ResponsePoller:
    """Background thread that polls the outbox and displays responses."""

    def __init__(self, agent_name, display):
        self.agent_name = agent_name
        self.display = display
        self.running = True
        self._prompt_pending = False
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self.running = False

    def _poll_loop(self):
        while self.running:
            try:
                responses = poll_outbox(self.agent_name)
                if responses:
                    # Clear current input line before printing
                    sys.stdout.write(f"\r{S.CLEAR_LINE}")
                    sys.stdout.flush()
                    for resp in responses:
                        self.display.show_response(resp)
                    # Reprint prompt
                    sys.stdout.write(make_prompt(self.agent_name))
                    sys.stdout.flush()
            except Exception as e:
                sys.stderr.write(f"[poller error] {e}\n")
            time.sleep(Config.poll_interval)


# ============================================================================
# BANNER AND PROMPT
# ============================================================================

def print_banner(agent_name, display):
    """Print startup banner."""
    w = min(term_width(), 70)
    border = f"{S.BOLD}{S.CYAN}{'=' * w}{S.RESET}"
    title = "asdaaas TUI"
    subtitle = f"Connected to: {agent_name}"

    print(f"\n{border}")
    print(f"{S.BOLD}{S.BRIGHT_CYAN}  {title}{S.RESET}")
    print(f"{S.CYAN}  {subtitle}{S.RESET}")
    print(f"{border}")
    print(f"{S.DIM}  Commands: /status  /gaze  /agents  /health  /clear  /quit{S.RESET}")
    print(f"{S.DIM}  All other input is sent to the agent as a message.{S.RESET}")
    print()


def make_prompt(agent_name):
    """Generate the input prompt string."""
    return f"{S.BOLD}{S.BLUE}{agent_name}{S.RESET}{S.BLUE} > {S.RESET}"


# ============================================================================
# SLASH COMMANDS
# ============================================================================

def handle_command(cmd, agent_name, display, status_bar):
    """Handle a TUI slash command. Returns True if handled, False otherwise."""
    parts = cmd.strip().split(None, 1)
    command = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if command == "/quit" or command == "/exit":
        return "quit"

    elif command == "/clear":
        os.system("clear")
        print_banner(agent_name, display)
        return True

    elif command == "/status":
        health = read_health(agent_name)
        if health:
            total = health.get("totalTokens", 0)
            window = health.get("contextWindow", 200000)
            pct = (total / window * 100) if window else 0
            display.show_system(f"Agent: {agent_name}")
            display.show_system(f"Status: {health.get('status', '?')}")
            display.show_system(f"Detail: {health.get('detail', '?')}")
            display.show_system(f"Context: {total:,}/{window:,} tokens ({pct:.1f}%)")
            display.show_system(f"PID: {health.get('pid', '?')}")
            display.show_system(f"Last activity: {health.get('last_activity', '?')}")
        else:
            display.show_error(f"No health data for {agent_name}")
        return True

    elif command == "/gaze":
        gaze = read_gaze(agent_name)
        if gaze:
            speech = gaze.get("speech", {})
            thoughts = gaze.get("thoughts", {})
            display.show_system(f"Speech -> {speech.get('target', '?')}/{speech.get('params', {}).get('room', '?')}")
            display.show_system(f"Thoughts -> {thoughts.get('target', '?')}/{thoughts.get('params', {}).get('room', '?')}")
        else:
            display.show_error(f"No gaze data for {agent_name}")
        return True

    elif command == "/health":
        health = read_health(agent_name)
        if health:
            display.show_system(json.dumps(health, indent=2))
        else:
            display.show_error("No health data")
        return True

    elif command == "/agents":
        # List all agents with health status
        agents_dir = Config.agents_home
        if agents_dir.exists():
            for entry in sorted(agents_dir.iterdir()):
                if entry.is_dir() and (entry / "asdaaas" / "health.json").exists():
                    h = read_health(entry.name)
                    if h:
                        total = h.get("totalTokens", 0)
                        window = h.get("contextWindow", 200000)
                        pct = (total / window * 100) if window else 0
                        status = h.get("status", "?")
                        marker = f"{S.GREEN}*{S.RESET}" if entry.name == agent_name else " "
                        display.show_system(f" {marker} {entry.name:<10s}  {status:<12s}  {pct:.0f}% ctx")
                    else:
                        display.show_system(f"   {entry.name:<10s}  offline")
        return True

    elif command == "/history":
        # Show recent history
        n = int(args) if args.isdigit() else 10
        recent = display.history[-n:]
        for ts, from_a, ctype, text in recent:
            preview = text[:80].replace('\n', ' ')
            display.show_system(f"  [{ts}] {from_a} ({ctype}): {preview}...")
        return True

    elif command == "/help":
        display.show_system("Commands:")
        display.show_system("  /status   - Show agent status and context usage")
        display.show_system("  /gaze     - Show agent's current gaze target")
        display.show_system("  /health   - Show raw health.json")
        display.show_system("  /agents   - List all agents with status")
        display.show_system("  /history  - Show recent message history")
        display.show_system("  /clear    - Clear screen")
        display.show_system("  /quit     - Exit TUI")
        display.show_system("")
        display.show_system("Everything else is sent to the agent as a message.")
        return True

    return False


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="asdaaas TUI -- rich terminal interface for agents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s --agent Trip
              %(prog)s --agent Sr --sender eric
              %(prog)s --agent Jr --agents-home ~/agents
        """))
    parser.add_argument("--agent", required=True, help="Agent name (e.g. Trip, Sr, Jr)")
    parser.add_argument("--agents-home", default=str(Config.agents_home),
                        help="Agents home directory (default: ~/agents)")
    parser.add_argument("--sender", default="eric",
                        help="Sender name for messages (default: eric)")
    args = parser.parse_args()

    Config.agents_home = Path(args.agents_home)
    agent_name = args.agent
    sender = args.sender

    # Ensure adapter directories exist
    ensure_dirs(agent_name)

    # Initialize components
    display = ResponseDisplay()
    status_bar = StatusBar(agent_name)
    poller = ResponsePoller(agent_name, display)

    # Check initial health
    health = read_health(agent_name)
    if health:
        display.show_system(f"Agent {agent_name}: {health.get('status', '?')} "
                           f"({health.get('totalTokens', 0):,} tokens)")
    else:
        display.show_error(f"No health data for {agent_name}. Agent may not be running.")

    # Print banner
    print_banner(agent_name, display)

    # Start background threads (start before draw so initial read populates data)
    status_bar.start()
    poller.start()

    # Draw initial status bar
    status_bar.draw()

    # Handle Ctrl+C
    def signal_handler(sig, frame):
        poller.stop()
        status_bar.stop()
        sys.stdout.write(f"\n{S.SHOW_CURSOR}")
        display.show_system("Goodbye!")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # Main input loop
    try:
        while True:
            try:
                user_input = input(make_prompt(agent_name))
            except EOFError:
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            # Handle slash commands
            if user_input.startswith("/"):
                result = handle_command(user_input, agent_name, display, status_bar)
                if result == "quit":
                    break
                elif result:
                    continue
                # Unknown command -- fall through to send as message

            # Send message to agent
            display.show_user(user_input, sender)
            msg_id = write_message(agent_name, user_input, sender=sender)
            display.show_system(f"sent ({msg_id[:8]})")

    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        status_bar.stop()
        sys.stdout.write(f"{S.SHOW_CURSOR}")
        display.show_system("Goodbye!")


if __name__ == "__main__":
    main()
