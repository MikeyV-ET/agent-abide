"""Chat scrollback widgets for asdaaas TUI."""
from __future__ import annotations

from textual.widgets import Static
from rich.markdown import Markdown as RichMarkdown
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.console import Console as RichConsole, Group

from theme import Theme


def short_ref(tool_id: str, prefix: str = "id") -> str:
    """Human-citable short id from toolCallId / task id / bell id."""
    if not tool_id:
        return ""
    s = tool_id.strip()
    if s.startswith("call-"):
        s = s[5:]
    if s.startswith("bell_"):
        return f"bell:{(s[5:13] if len(s) > 13 else s[5:])}"
    chunk = s.replace("_", "-").split("-")[0]
    if len(chunk) > 10:
        chunk = chunk[:8]
    return f"{prefix}:{chunk}" if chunk else ""


def _flatten_to_text(renderable, width: int = 120) -> Text:
    """Render a Rich renderable through Console, return as Text for native selectability.

    Strips trailing whitespace on lines without background color."""
    from io import StringIO
    from rich.style import Style as RichStyle
    buf = StringIO()
    console = RichConsole(file=buf, force_terminal=True, width=width, no_color=False)
    console.print(renderable, end="")
    result = Text.from_ansi(buf.getvalue())
    lines = result.split("\n")
    for line in lines:
        plain = line.plain
        stripped_len = len(plain.rstrip())
        if stripped_len < len(plain):
            has_bg = any(
                end > stripped_len and RichStyle.parse(str(s)).bgcolor
                for start, end, s in line._spans
            )
            if not has_bg:
                line.rstrip()
    return Text("\n").join(lines)


class ToolCallPanel(Static):
    """Tool call panel: default snippet view, full body on click expand.

    Display policy (Eric 2026-08-04): tools are secondary to thinking; Grok-4.5
    tool dumps should not dominate scrollback. Default = small snippet + expand.
    """

    SNIPPET_LINES = 4
    MAX_EXPANDED_LINES = 80
    MAX_STORED_CHARS = 65536
    MAX_ACTIVE_LINES = 15  # legacy alias

    def __init__(self, tool_id: str, title: str, kind: str = "", ts: str = "", **kwargs):
        super().__init__(**kwargs)
        self.tool_id = tool_id
        self.tool_title = title
        self.tool_kind = kind
        self.tool_status = "running"
        self.tool_output = ""
        self.tool_ts = ts or ""
        self.border_title = title.replace("[", "\\[")
        self._collapsed = True  # snippet mode by default
        self._mounted_interjections: set[str] = set()

    def _cap_output(self, content: str) -> str:
        if len(content) <= self.MAX_STORED_CHARS:
            return content
        keep = self.MAX_STORED_CHARS - 80
        return f"[… truncated {len(content) - keep} chars …]\n" + content[-keep:]

    def set_status(self, status: str):
        self.tool_status = status
        if status in ("completed", "failed"):
            self._collapsed = True
        self.refresh(layout=True)

    def set_output(self, content: str):
        self.tool_output = self._cap_output(content)
        if self._collapsed:
            self.refresh()
        else:
            self.refresh(layout=True)

    def append_output(self, content: str):
        self.tool_output = self._cap_output(self.tool_output + content)
        if self._collapsed:
            self.refresh()
        else:
            self.refresh(layout=True)

    def on_click(self, event) -> None:
        """Toggle snippet vs full body."""
        self._collapsed = not self._collapsed
        self.refresh(layout=True)

    def render(self):
        if self.tool_status == "completed":
            status_icon = "✓"
            border_style = Theme.BR_GREEN
        elif self.tool_status == "failed":
            status_icon = "✗"
            border_style = Theme.BR_RED
        elif self.tool_status == "in_progress":
            status_icon = "⟳"
            border_style = Theme.BR_YELLOW
        else:
            status_icon = "…"
            border_style = Theme.BR_BLUE

        kind_icons = {
            "read": "📖", "execute": "⚡", "edit": "✏️",
            "search": "🔍", "think": "💭", "other": "📋",
        }
        kind_icon = kind_icons.get(self.tool_kind, "🔧")
        # Prefer a real title; generic "tool" is useless for reference
        label = (self.tool_title or "").strip() or (self.tool_kind or "tool")
        if label.lower() in ("tool", "unknown tool", "unknown"):
            label = self.tool_kind or "tool"
        ref = short_ref(self.tool_id)
        # Prefer clock time for human citation ("the 15:08 tool"); keep short id as backup
        cite = getattr(self, "tool_ts", "") or ref
        if cite and ref and getattr(self, "tool_ts", ""):
            title = f"{kind_icon} {label} · {getattr(self, "tool_ts", "")} · {ref} {status_icon}"
        elif cite:
            title = f"{kind_icon} {label} · {cite} {status_icon}"
        else:
            title = f"{kind_icon} {label} {status_icon}"

        from textual.color import Color as TextualColor
        try:
            color = TextualColor.parse(border_style)
        except Exception:
            color = TextualColor.parse("blue")

        lines = self.tool_output.split("\n") if self.tool_output else []
        n_lines = len(lines) if self.tool_output else 0

        if self._collapsed:
            self.styles.border = ("round", color)
            self.styles.padding = (0, 1)
            self.border_title = title.replace("[", "\\[")
            body = Text()
            if not self.tool_output:
                ref = short_ref(self.tool_id)
        cite = getattr(self, "tool_ts", "") or ref
                empty = f"(no output yet — cite {cite})" if cite else "(no output yet)"
                body.append(empty, style=f"italic {Theme.DARK4}")
            else:
                snippet = lines[: self.SNIPPET_LINES]
                body.append("\n".join(snippet), style=Theme.GRAY)
                hidden = n_lines - len(snippet)
                if hidden > 0 or len(self.tool_output) > 200:
                    body.append(
                        f"\n  ▸ +{max(hidden, 0)} lines — click to expand",
                        style=Theme.DARK4,
                    )
                else:
                    body.append("\n  ▸ click to expand", style=Theme.DARK4)
            return body

        self.styles.border = ("round", color)
        self.styles.padding = (0, 1)
        self.border_title = (title + " [expanded — click to collapse]").replace("[", "\\[")

        if self.tool_output:
            if n_lines > self.MAX_EXPANDED_LINES:
                display = "\n".join(
                    lines[:40] + [f"... ({n_lines - 60} lines) ..."] + lines[-20:]
                )
                content = Text(display, style=Theme.GRAY)
            else:
                content = Text(self.tool_output, style=Theme.GRAY)
        else:
            content = Text("(no output)", style=f"italic {Theme.DARK4}")
        return content

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
            "completed": f"[{Theme.BR_GREEN}]✓[/]",
            "in_progress": f"[{Theme.BR_YELLOW}]▶[/]",
            "pending": f"[{Theme.GRAY}]○[/]",
            "cancelled": f"[{Theme.GRAY}]✗[/]",
        }

        for entry in self.entries:
            icon = status_icons.get(entry.get("status", "pending"), "?")
            content = entry.get("content", "")
            style = "dim" if entry.get("status") == "completed" else ""
            table.add_row(icon, Text(content, style=style))

        return Panel(table, title="📋 Plan", title_align="left",
                     border_style=Theme.BR_PURPLE, padding=(0, 1))


def is_system_reminder(text: str) -> bool:
    """True if this user_message_chunk is harness chrome, not operator speech."""
    if not text:
        return False
    s = text.lstrip()
    return s.startswith("<system-reminder>") or s.startswith("<system_reminder>")


class SystemReminderPanel(Static):
    """Collapsed-by-default panel for <system-reminder> blobs (like tool panels).

    These arrive as user_message_chunk but are not Eric turns — background task
    notices, binary reminders, etc. Snippet + expand on click.
    """

    SNIPPET_LINES = 3
    MAX_EXPANDED_LINES = 40

    def __init__(self, text: str, **kwargs):
        super().__init__(**kwargs)
        self._text = text or ""
        self._collapsed = True
        self._title = self._make_title(self._text)

    @staticmethod
    def _make_title(text: str) -> str:
        import re
        t = text or ""
        m = re.search(r'Background task\s+"([^"]+)"\s+completed', t)
        if m:
            short = m.group(1)
            if len(short) > 24:
                short = short[:24] + "…"
            return f"system: task {short} completed"
        m = re.search(r"<system-reminder>\s*([^\n<]{1,60})", t)
        if m:
            return f"system: {m.group(1).strip()[:50]}"
        return "system-reminder"

    def on_click(self, event) -> None:
        self._collapsed = not self._collapsed
        self.refresh(layout=True)

    def render(self):
        from textual.color import Color as TextualColor
        border_style = Theme.DARK4
        try:
            color = TextualColor.parse(border_style)
        except Exception:
            color = TextualColor.parse("gray")

        lines = self._text.split("\n") if self._text else []
        title = f"📎 {self._title}"
        if self._collapsed:
            self.styles.border = ("round", color)
            self.styles.padding = (0, 1)
            self.border_title = title.replace("[", "\\[")
            body = Text()
            snippet = lines[: self.SNIPPET_LINES]
            body.append("\n".join(snippet), style=Theme.DARK4)
            hidden = max(0, len(lines) - len(snippet))
            if hidden or len(self._text) > 200:
                body.append(f"\n  ▸ +{hidden} lines — click to expand", style=Theme.DARK4)
            else:
                body.append("\n  ▸ click to expand", style=Theme.DARK4)
            return body

        self.styles.border = ("round", color)
        self.styles.padding = (0, 1)
        self.border_title = (title + " [expanded]").replace("[", "\\[")
        if len(lines) > self.MAX_EXPANDED_LINES:
            display = "\n".join(
                lines[:25] + [f"... ({len(lines) - 35} lines) ..."] + lines[-10:]
            )
        else:
            display = self._text
        return Text(display, style=Theme.DARK4)



class UserMessage(Static):
    """User message display -- clean inline style with chevron prefix."""

    def __init__(self, text: str, **kwargs):
        super().__init__(**kwargs)
        self.user_text = text

    def render(self) -> Text:
        text = Text()
        text.append("❯ ", style=f"bold {Theme.BR_BLUE}")
        text.append(self.user_text, style=Theme.FG)
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
        # Streaming: repaint only — full layout on every chunk starves input/scroll
        self.refresh()

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

    @staticmethod
    def _format_ephacts(text: str) -> str:
        """Replace <ephact> blocks with visible markdown blockquotes so they render inline."""
        import re
        if "<ephact" not in text:
            return text
        def _repl(m):
            etype = m.group(1)
            title = m.group(2)
            body = m.group(3).strip()
            label = f"📌 {title}" if title else f"📌 {etype}"
            lines = body.split("\n")
            quoted = "\n".join(f"> {line}" for line in lines)
            return f"\n> **{label}**\n{quoted}\n"
        return re.sub(
            r'<ephact\s+type=["\'](\w+)["\'](?:\s+title=["\']([^"\']*)["\'])?\s*>(.*?)</ephact>',
            _repl, text, flags=re.DOTALL)

    def render(self):
        text = self._format_interjections(self._text)
        text = self._format_ephacts(text)
        w = self.size.width - 2 if self.size.width > 10 else 120
        return _flatten_to_text(RichMarkdown(text), width=w)

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
        # Keep thinking visible; avoid layout thrash on every thought chunk
        self.refresh()

    def on_click(self, event) -> None:
        """Toggle expanded/collapsed state."""
        self._expanded = not self._expanded
        self.refresh(layout=True)

    def render(self):
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
            title_str += " \\[expanded — click to collapse]"

        self.border_title = title_str
        return Text(display, style=Theme.DARK4)

class InterjectionBlock(Static):
    """Renders an interjection message as a distinct panel, styled like ThinkingBlock."""

    def __init__(self, message: str, **kwargs):
        super().__init__(**kwargs)
        self._message = message
        self._ref = short_ref(message, prefix="bell") if "id=bell_" in (message or "") or "(id=" in (message or "") else ""
        # pull id=bell_xxx from message
        import re
        m = re.search(r"id=(bell_[a-zA-Z0-9_]+)", message or "")
        if m:
            self._ref = short_ref(m.group(1))

    def render(self):
        if self._ref:
            self.border_title = f"🔔 Interjection · {self._ref}"
        else:
            self.border_title = "🔔 Interjection"
        return Text(self._message, style=Theme.BR_ORANGE)


# =============================================================================
# Operator Identity Screen
# =============================================================================

