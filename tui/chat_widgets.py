"""Chat scrollback widgets for asdaaas TUI."""
from __future__ import annotations

from textual.widgets import Static
from textual.containers import Vertical, Horizontal
from rich.markdown import Markdown as RichMarkdown
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.console import Console as RichConsole, Group

from theme import Theme
from nav_widgets import layout_bubble, layout_painted_bubble, nick_mark, bubble_metrics
import re


def operator_body(text: str) -> str:
    """Strip envelope/context tag for paint. Does not change the record."""
    t = (text or "").strip()
    t = re.sub(r"^\[background\][^\n]*\n?", "", t)
    t = re.sub(r"^<eric\b[^>]*>\s*", "", t, flags=re.I)
    t = re.sub(r"^\[eric\b[^\]]*\]\s*", "", t, flags=re.I)
    t = re.sub(r"^\[IRC [^\]]*\]\s*", "", t)
    t = re.sub(r"\n\[Context left[^\]]*\]\s*$", "", t)
    return t.strip()


def fill_line_to_width(line: Text, inner: int, fill: str) -> Text:
    """Paint remaining cells so the compositor cannot skip-to-border.

    Collapsed tool one-liners were a short string then a cursor skip to
    the round-box │. Windows xterm indexed that skip wrong; speech
    bubbles (every cell has bg) never did. Expanded tools are dense.
    """
    if inner <= 0:
        return line
    padn = max(0, int(inner) - line.cell_len)
    if padn:
        line.append(" " * padn, style=f"on {fill}")
    return line


def telemetry_from_chunk(text: str) -> str:
    m = re.search(r"\[Context left[^\]]*\]", text or "")
    if not m:
        return ""
    return m.group(0).strip("[]").replace("Context left ", "")


def via_from_chunk(text: str) -> str:
    t = text or ""
    m = re.search(r"reply_via=([a-z0-9_-]+)", t, re.I)
    if m:
        v = m.group(1).lower().replace("_outbox", "").strip("_")
        return v or "irc"
    if t.startswith("[background]") or "[IRC #" in t:
        return "irc"
    m = re.search(r"\(via\s+([a-z0-9_-]+)\)", t, re.I)
    if m:
        return m.group(1).lower()
    return "tui"


def is_control_user_line(text: str) -> bool:
    """asdaaas chrome (continue / aa.control / localmail / compact / session-limit), not an operator turn."""
    if not text or not isinstance(text, str):
        return False
    s = text.strip()
    if not s:
        return False
    body = s
    footer_re = re.compile(r"\n?\[context left[^\]]*\]\s*$", re.IGNORECASE)
    while True:
        nxt = footer_re.sub("", body).rstrip()
        if nxt == body:
            break
        body = nxt
    if not body:
        return True
    blow = body.lower()
    if blow.startswith("[continue") or blow.startswith("[delay") or blow.startswith("[aa.control"):
        return True
    if blow.startswith("[localmail") or blow.startswith("[from:"):
        return True
    if "mail from" in blow[:80] and "localmail" in blow[:80]:
        return True
    if blow.startswith("[session:compact") or blow.startswith("[compaction"):
        return True
    if blow.startswith("[compaction complete") or "follow your boot protocol" in blow[:240]:
        return True
    if blow.startswith("this session is being continued"):
        return True
    if "your turn ended" in blow and "stand by" in blow:
        return True
    if re.match(r"^you(?:'ve| have) hit your session limit\b", blow):
        return True
    if blow.startswith("[context left"):
        return True
    return False


def system_alert_body(text: str, cap: int = 800) -> str:
    """Paint-only: peel context footer, cap length. Record stays raw."""
    t = (text or "").strip()
    t = re.sub(r"\n\[context left[^\]]*\]\s*$", "", t, flags=re.I).strip()
    if len(t) > cap:
        t = t[:cap].rstrip() + "…"
    return t


def make_user_message(text: str, *, echo: bool = False) -> "UserMessage":
    """Paint-only: eric (tui|irc) + body + dim telemetry. Record stays raw."""
    via = via_from_chunk(text) if not echo else "tui"
    nick = f"eric ({via})"
    body = operator_body(text) if not echo else (text or "").strip()
    um = UserMessage(body or (text or "").strip(), nick=nick, via=via)
    tel = telemetry_from_chunk(text)
    if tel:
        um.mark_received(tel)
    return um


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

    Strips trailing whitespace on lines without background color.
    Strips Rich blockquote bars (▌) so select/copy gets prose, not the bar chrome.
    """
    from io import StringIO
    from rich.style import Style as RichStyle
    buf = StringIO()
    console = RichConsole(file=buf, force_terminal=True, width=width, no_color=False)
    console.print(renderable, end="")
    result = Text.from_ansi(buf.getvalue())
    lines = result.split("\n")
    cleaned: list[Text] = []
    for line in lines:
        plain = line.plain
        # Rich MD blockquotes: leading ▌ (U+258C), often with a following space
        if "▌" in plain[:4] or plain.lstrip().startswith("▌"):
            s = plain
            i = 0
            while i < len(s) and s[i] == " ":
                i += 1
            if i < len(s) and s[i] == "▌":
                i += 1
                if i < len(s) and s[i] == " ":
                    i += 1
            rest = s[i:]
            new_line = Text()
            if rest:
                new_line.append("  ")
                new_line.append(rest, style=Theme.FG)
            cleaned.append(new_line)
            continue
        stripped_len = len(plain.rstrip())
        if stripped_len < len(plain):
            has_bg = any(
                end > stripped_len and RichStyle.parse(str(s)).bgcolor
                for start, end, s in line._spans
            )
            if not has_bg:
                line.rstrip()
        cleaned.append(line)
    return Text("\n").join(cleaned)



class ToolCallPanel(Static):
    """Tool call panel: command sticky + output snippet; full body on expand.

    Display policy (Eric 2026-08-04 / 2026-09-20): tools secondary to thinking;
    keep *both* the command and the return so you can see what ran.
    """

    DEFAULT_CSS = """
    ToolCallPanel {
        width: 80%;
        margin: 0 2 1 1;
        align: left top;
    }
    """

    SNIPPET_LINES = 4
    SNIPPET_MAX_CHARS = 480
    SNIPPET_MAX_VISUAL_ROWS = 4
    MAX_EXPANDED_LINES = 80
    MAX_STORED_CHARS = 65536
    MAX_ACTIVE_LINES = 15
    COMMAND_MAX_CHARS = 8000

    def __init__(self, tool_id: str, title: str, kind: str = "", ts: str = "", **kwargs):
        super().__init__(**kwargs)
        self.tool_id = tool_id
        self.tool_title = title
        self.tool_kind = kind
        self.tool_status = "running"
        self.tool_command = ""  # sticky: what was run (never wiped by set_output)
        self.tool_output = ""
        self.tool_ts = ts or ""
        self.border_title = title.replace("[", "\\[")
        self.density = "snippet"  # one | snippet | full
        self._mounted_interjections: set[str] = set()

    def _cap_output(self, content: str) -> str:
        if len(content) <= self.MAX_STORED_CHARS:
            return content
        keep = self.MAX_STORED_CHARS - 80
        return f"[… truncated {len(content) - keep} chars …]\n" + content[-keep:]

    def set_command(self, command: str) -> None:
        """Record the invocation (args / shell). Sticky across set_output."""
        if not command or not str(command).strip():
            return
        first = str(command).strip()
        if len(first) > self.COMMAND_MAX_CHARS:
            # middle ellipsis already applied by compact helper; hard cap
            first = first[: self.COMMAND_MAX_CHARS - 1] + "…"

        def _weak(s: str) -> bool:
            low = s.lower().rstrip(" …")
            return low in ("run_terminal_command", "tool", "") or (
                low.startswith("cd ") and "&&" not in low and "sed" not in low and "rg" not in low
            )

        if self.tool_command:
            if _weak(first) and not _weak(self.tool_command):
                return
            # Prefer longer/more complete summaries (full script > single line)
            if len(first) + 10 < len(self.tool_command) and first.split("&&")[0].strip() in self.tool_command:
                return
        self.tool_command = first
        self.refresh(layout=True)

    @property
    def _collapsed(self) -> bool:
        return self.density != "full"

    @_collapsed.setter
    def _collapsed(self, val: bool) -> None:
        if val:
            if self.density == "full":
                self.density = "snippet"
        else:
            self.density = "full"

    def _run_stack(self):
        w = self.parent
        while w is not None:
            if isinstance(w, ToolRunStack):
                return w
            w = getattr(w, "parent", None)
        return None

    def set_status(self, status: str):
        self.tool_status = status
        if status in ("completed", "failed") and self.density != "full":
            self.density = "one"
        self.refresh(layout=True)
        stack = self._run_stack()
        if stack is not None:
            stack._apply_densities()

    def set_output(self, content: str):
        """Set return/stdout. Does **not** clear tool_command."""
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
        if self.density == "one":
            self.density = "snippet"
        elif self.density == "snippet":
            self.density = "full"
        else:
            self.density = "one"
        self.refresh(layout=True)
        if hasattr(event, "stop"):
            event.stop()

    def _collapsed_snippet(self, raw: str | None = None) -> tuple[str, bool]:
        raw = self.tool_command if raw is None and self.tool_command else (raw if raw is not None else (self.tool_output or ""))
        if not raw:
            return "", False
        width = max(40, (self.size.width or 80) - 6)
        max_rows = self.SNIPPET_MAX_VISUAL_ROWS
        out: list[str] = []
        visual = 0
        more = False
        for ln in raw.split("\n"):
            chunks = max(1, (len(ln) + width - 1) // width) if ln else 1
            if visual + chunks > max_rows:
                remain = max_rows - visual
                if remain > 0:
                    cap = remain * width
                    bit = ln[: max(0, cap - 1)].rstrip()
                    out.append(bit + "…")
                more = True
                break
            out.append(ln)
            visual += chunks
        else:
            if len(raw.split("\n")) > len(out):
                more = True
        piece = "\n".join(out)
        if len(raw.split("\n")) > len(out) or len(raw) > len(piece):
            more = True
        return piece, more

    def _title_line(self, status_icon: str) -> str:
        kind_icons = {
            "read": "📖", "execute": "⚡", "edit": "✏️",
            "search": "🔍", "think": "💭", "other": "📋",
        }
        kind_icon = kind_icons.get(self.tool_kind, "🔧")
        # Prefer sticky command in the border when we have it
        label = (self.tool_command or self.tool_title or "").strip() or (
            self.tool_kind or "tool"
        )
        if label.lower() in ("tool", "unknown tool", "unknown", "run_terminal_command"):
            label = self.tool_command or self.tool_kind or "tool"
        # Keep border readable
        if len(label) > 60:
            label = label[:57] + "…"
        ref = short_ref(self.tool_id)
        ts = self.tool_ts or ""
        if ts and ref:
            return f"{kind_icon} {label} · {ts} · {ref} {status_icon}"
        if ts:
            return f"{kind_icon} {label} · {ts} {status_icon}"
        if ref:
            return f"{kind_icon} {label} · {ref} {status_icon}"
        return f"{kind_icon} {label} {status_icon}"

    def _one_line_inner_width(self) -> int:
        try:
            w = int(self.size.width)
            if w > 1:
                return w
        except Exception:
            pass
        stack = self._run_stack()
        if stack is not None:
            try:
                sw = int(stack.size.width)
                if sw > 8:
                    return max(1, sw - 5)
            except Exception:
                pass
        return 0

    def on_mount(self) -> None:
        def _again():
            if self.density == "one":
                self.refresh()
        try:
            self.call_after_refresh(_again)
        except Exception:
            pass

    def on_resize(self, event=None) -> None:
        if self.density == "one":
            self.refresh()

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

        title = self._title_line(status_icon)

        from textual.color import Color as TextualColor
        try:
            color = TextualColor.parse(border_style)
        except Exception:
            color = TextualColor.parse("blue")

        lines = self.tool_output.split("\n") if self.tool_output else []
        n_lines = len(lines) if self.tool_output else 0

        def _prepend_command(body: Text) -> None:
            if self.tool_command:
                body.append(f"$ {self.tool_command}\n", style=f"bold {Theme.FG}")

        if self.density == "one":
            try:
                self.styles.border = ("none", color)
            except Exception:
                self.styles.border = None
            self.styles.padding = (0, 0)
            self.styles.margin = (0, 0, 0, 0)
            try:
                self.styles.height = 1
            except Exception:
                pass
            self.border_title = ""
            cmd = (self.tool_command or self.tool_title or self.tool_kind or "tool").strip()
            cmd = " ".join(cmd.split())
            if len(cmd) > 56:
                cmd = cmd[:55] + "…"
            kind_icons = {
                "read": "📖", "execute": "⚡", "edit": "✏️",
                "search": "🔍", "think": "💭", "other": "📋",
            }
            kicon = kind_icons.get(self.tool_kind, "🔧")
            ref = short_ref(self.tool_id)
            line = Text()
            line.append(f"{status_icon} ", style=f"bold {border_style}")
            line.append(f"{kicon} {cmd}", style=Theme.FG)
            if ref:
                line.append(f"  {ref}", style=Theme.DARK4)
            line.append("  ▸", style=Theme.DARK4)
            fill_line_to_width(line, self._one_line_inner_width(), Theme.DARK1)
            return line

        if self.density != "full":
            try:
                self.styles.height = "auto"
            except Exception:
                pass
            self.styles.border = ("round", color)
            self.styles.padding = (0, 1)
            if self._run_stack() is not None:
                self.styles.margin = (0, 0, 0, 0)
                self.styles.border = ("none", color)
                self.styles.padding = (0, 0)
            self.border_title = title.replace("[", "\\[")
            body = Text()
            src = self.tool_command or self.tool_output or ""
            if src:
                snippet, more = self._collapsed_snippet(src)
                style = f"bold {Theme.FG}" if self.tool_command else Theme.GRAY
                body.append(snippet, style=style)
                nsrc = len(src.split("\n"))
                hidden = max(0, nsrc - self.SNIPPET_LINES)
                if more or hidden:
                    if hidden > 0:
                        body.append(
                            f"\n  ▸ +{hidden} lines — click to expand",
                            style=Theme.DARK4,
                        )
                    else:
                        body.append(
                            "\n  ▸ truncated — click to expand",
                            style=Theme.DARK4,
                        )
                else:
                    body.append("\n  ▸ click to expand", style=Theme.DARK4)
            else:
                cite = self.tool_ts or short_ref(self.tool_id)
                empty = f"(no output yet — cite {cite})" if cite else "(no output yet)"
                body.append(empty, style=f"italic {Theme.DARK4}")
            return body

        self.styles.border = ("round", color)
        self.styles.padding = (0, 1)
        self.border_title = (title + " [expanded — click to collapse]").replace("[", "\\[")

        body = Text()
        _prepend_command(body)
        if self.tool_output:
            if n_lines > self.MAX_EXPANDED_LINES:
                display = "\n".join(
                    lines[:40] + [f"... ({n_lines - 60} lines) ..."] + lines[-20:]
                )
                body.append(display, style=Theme.GRAY)
            else:
                body.append(self.tool_output, style=Theme.GRAY)
        else:
            body.append("(no output)", style=f"italic {Theme.DARK4}")
        return body



def tool_run_window(panels, *, max_rows: int = 8, snippet_cost: int = 5):
    """Which tools are visible in a run. Oldest finished hide behind +N.

    Live tools are always visible (snippet). Finished fill leftover rows as
    one-liners (cost 1). panels is chronological (oldest first).
    """
    live, done = [], []
    for p in panels:
        st = getattr(p, "tool_status", "") or ""
        if st in ("completed", "failed"):
            done.append(p)
        else:
            live.append(p)
    live_cost = snippet_cost if live else 0
    fin_slots = max_rows if not live else max(0, max_rows - live_cost)
    hidden = max(0, len(done) - fin_slots)
    visible_done = done[hidden:]
    return hidden, visible_done + live


class ToolRunGutter(Static):
    """Left ▸ / ▾ — expands the run to 4+1 summaries."""

    DEFAULT_CSS = """
    ToolRunGutter {
        width: 3;
        height: auto;
        padding: 0;
    }
    """

    def __init__(self, stack: "ToolRunStack", **kwargs):
        super().__init__(**kwargs)
        self._stack = stack

    def on_click(self, event) -> None:
        self._stack.toggle_run()
        if hasattr(event, "stop"):
            event.stop()

    def render(self) -> Text:
        n = self._stack.panel_count()
        mark = "▾" if self._stack._run_open else "▸"
        t = Text()
        t.append(f" {mark}", style=f"bold {Theme.BR_AQUA}")
        if n:
            t.append(f"\n {n}", style=Theme.DARK4)
        return t


class ToolRunOverflow(Static):
    """+N earlier — click reveals hidden finished tools."""

    DEFAULT_CSS = """
    ToolRunOverflow {
        width: 100%;
        height: auto;
        padding: 0;
    }
    """

    def __init__(self, stack: "ToolRunStack", **kwargs):
        super().__init__(**kwargs)
        self._stack = stack

    def on_click(self, event) -> None:
        self._stack.reveal_hidden()
        if hasattr(event, "stop"):
            event.stop()

    def render(self) -> Text:
        n = self._stack._hidden
        return Text(f"  +{n} earlier — click to show", style=Theme.DARK4)


class ToolRunStack(Horizontal):
    """Consecutive tools: one-liners + live 4+1, left ▸, ~8-line cap."""

    DEFAULT_CSS = """
    ToolRunStack {
        width: 80%;
        height: auto;
        margin: 0 2 1 1;
        align: left top;
        padding: 0 1;
    }
    ToolRunStack ToolCallPanel {
        width: 100%;
        height: auto;
        margin: 0;
        padding: 0;
    }
    .tool-run-body {
        width: 1fr;
        height: auto;
        padding: 0;
    }
    ToolRunOverflow {
        margin: 0;
        padding: 0;
        height: 1;
    }
    """

    MAX_ROWS = 8
    SNIPPET_COST = 5

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._run_open = False
        self._reveal = False
        self._hidden = 0
        self._pending: list = []
        self._gutter: ToolRunGutter | None = None
        self._body: Vertical | None = None
        self._overflow: ToolRunOverflow | None = None

    def compose(self):
        self._gutter = ToolRunGutter(self)
        yield self._gutter
        self._body = Vertical(classes="tool-run-body")
        self._overflow = ToolRunOverflow(self)
        with self._body:
            yield self._overflow

    def on_mount(self) -> None:
        try:
            from textual.color import Color as TextualColor
            self.styles.border = ("round", TextualColor.parse(Theme.DARK3))
        except Exception:
            self.styles.border = ("round", "gray")
        self.styles.padding = (0, 1)
        self._flush_pending()

    def panel_count(self) -> int:
        return len(self._panels())

    def _panels(self) -> list:
        if self._body is None:
            return list(self._pending)
        return [c for c in self._body.children if isinstance(c, ToolCallPanel)]

    def add_panel(self, panel: "ToolCallPanel") -> None:
        self._pending.append(panel)
        if self._body is not None and getattr(self._body, "is_attached", False):
            self._flush_pending()

    def _flush_pending(self) -> None:
        if self._body is None or not getattr(self._body, "is_attached", False):
            return
        for p in self._pending:
            try:
                p.styles.margin = (0, 0, 0, 0)
                p.styles.padding = (0, 0, 0, 0)
            except Exception:
                pass
            self._body.mount(p)
        self._pending.clear()
        self._apply_densities()

    def toggle_run(self) -> None:
        self._run_open = not self._run_open
        self._apply_densities()
        if self._gutter is not None:
            self._gutter.refresh()

    def reveal_hidden(self) -> None:
        self._reveal = True
        self._apply_densities()

    def _apply_densities(self) -> None:
        panels = self._panels()
        if not panels:
            self._hidden = 0
            if self._overflow is not None:
                self._overflow.display = False
            return
        if self._reveal:
            hidden, visible = 0, panels
        else:
            hidden, visible = tool_run_window(
                panels, max_rows=self.MAX_ROWS, snippet_cost=self.SNIPPET_COST
            )
        self._hidden = hidden
        vis = set(id(p) for p in visible)
        for p in panels:
            show = id(p) in vis
            p.display = show
            if not show:
                continue
            st = p.tool_status or ""
            live = st not in ("completed", "failed")
            if p.density == "full":
                continue
            if live:
                p.density = "snippet"
            else:
                p.density = "snippet" if self._run_open else "one"
            p.refresh(layout=True)
        if self._overflow is not None:
            self._overflow.display = hidden > 0
            if hidden > 0:
                self._overflow.refresh()
        if self._gutter is not None:
            self._gutter.refresh()



class PlanPanel(Static):
    """Renders the agent's todo/plan list."""

    DEFAULT_CSS = """
    PlanPanel {
        width: 80%;
        margin: 0 2 1 1;
        align: left top;
    }
    """

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
            if not isinstance(entry, dict):
                entry = {"content": str(entry), "status": "pending"}
            icon = status_icons.get(entry.get("status", "pending"), "?")
            content = entry.get("content", "")
            style = "dim" if entry.get("status") == "completed" else ""
            table.add_row(icon, Text(str(content), style=style))

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
    """Operator turn — same grow-left bubble as IRC room human lines."""

    DEFAULT_CSS = """
    UserMessage {
        height: auto;
        width: 100%;
        padding: 0;
        margin: 0 2 1 0;
    }
    """

    def __init__(self, text: str, nick: str = "eric", via: str = "tui", **kwargs):
        super().__init__(**kwargs)
        self.user_text = text
        self._nick = nick or "eric"
        self._via = via or "tui"
        self._received = False
        self._telemetry = ""

    def on_resize(self) -> None:
        self.refresh()

    def set_text(self, text: str) -> None:
        self.user_text = text
        self.refresh(layout=True)

    def mark_received(self, telemetry: str = "") -> None:
        self._received = True
        if telemetry:
            self._telemetry = telemetry.strip()
        self.refresh(layout=True)

    def copy_payload(self) -> str:
        return (self.user_text or "").strip()

    def on_click(self, event) -> None:
        text = self.copy_payload()
        if not text:
            return
        try:
            self.app.copy_to_clipboard(text)
            self.app.notify(f"copied user turn ({len(text)} chars)", severity="information", timeout=2)
        except Exception:
            pass
        if hasattr(event, "stop"):
            event.stop()

    def render(self) -> Text:
        mark, color = nick_mark(self._nick)
        inner = Text()
        inner.append(f"{mark} ", style=f"bold {color}")
        inner.append(f"{self._nick}  ", style=f"bold {color}")
        if getattr(self, "_via", "tui") != "tui":
            inner.append("· background  ", style=f"italic {Theme.BR_ORANGE}")
        inner.append(self.user_text, style=Theme.FG)
        if getattr(self, "_received", False):
            inner.append("  ✓", style=Theme.DARK4)
        tel = getattr(self, "_telemetry", "") or ""
        if tel:
            inner.append("\n")
            inner.append(tel, style=Theme.DARK4)
        total = self.size.width if self.size.width >= 40 else 80
        return layout_painted_bubble(
            inner, is_human=True, total=total, border=color, fill=Theme.DARK2
        )

class AgentMessage(Static):
    """Agent message display — markdown in the left 75% bubble."""

    DEFAULT_CSS = """
    AgentMessage {
        height: auto;
        width: 100%;
        padding: 0;
        margin: 0 2 1 0;
    }
    """

    def __init__(self, nick: str = "", **kwargs):
        super().__init__(**kwargs)
        self._chunks: list[str] = []
        self._text = ""
        self._nick = nick or ""

    def append_chunk(self, text: str):
        # Keep a single growing string; chunk lists explode on multi-day streams
        # and force O(n) join on every token.
        self._chunks.append(text)
        self._text += text
        if len(self._chunks) > 32:
            self._chunks = [self._text]
        # Fast path: repaint only (prod-like latency). Layout every N chunks so
        # height grows; full layout+scroll on turn/tool boundary via flush.
        n = getattr(self, "_chunk_n", 0) + 1
        self._chunk_n = n
        if n % 12 == 0:
            self.refresh(layout=True)
        else:
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
        """Replace <ephact> blocks with inline markdown. Tables are NOT blockquoted:
        quoting + flatten(width=4000) padded them into dozens of empty bubble rows.
        """
        import re
        if "<ephact" not in text:
            return text
        def _repl(m):
            etype = m.group(1)
            title = m.group(2)
            body = m.group(3).strip()
            label = f"📌 {title}" if title else f"📌 {etype}"
            if etype == "table":
                return f"\n\n**{label}**\n\n{body}\n"
            lines = body.split("\n")
            quoted = "\n".join(f"> {line}" for line in lines)
            return f"\n> **{label}**\n{quoted}\n"
        return re.sub(
            r'<ephact\s+type=["\'](\w+)["\'](?:\s+title=["\']([^"\']*)["\'])?\s*>(.*?)</ephact>',
            _repl, text, flags=re.DOTALL)

    def on_resize(self) -> None:
        self.refresh()

    def copy_payload(self) -> str:
        return (self._text or "").strip()

    def on_click(self, event) -> None:
        text = self.copy_payload()
        if not text:
            return
        try:
            self.app.copy_to_clipboard(text)
            self.app.notify(f"copied agent turn ({len(text)} chars)", severity="information", timeout=2)
        except Exception:
            pass
        if hasattr(event, "stop"):
            event.stop()

    def render(self):
        text = self._format_interjections(self._text)
        text = self._format_ephacts(text)
        total = self.size.width if self.size.width >= 40 else 80
        _, _, _, col = bubble_metrics(total)
        inner_col = max(24, col - 2)
        # Prose: flatten wide so markdown does not pre-wrap. Tables in ephacts
        # must flatten at bubble width or Rich pads them to thousands of cells.
        md_width = inner_col if "📌" in text or "|" in text else 4000
        md = _flatten_to_text(RichMarkdown(text), width=md_width)
        nick = self._nick
        if not nick:
            try:
                nick = self.app._active_agent or "agent"
            except Exception:
                nick = "agent"
        mark, color = nick_mark(nick)
        inner = Text()
        inner.append(f"{mark} ", style=f"bold {color}")
        inner.append(f"{nick}  ", style=f"bold {color}")
        inner.append(md)
        return layout_painted_bubble(
            inner, is_human=False, total=total, border=color, fill=Theme.DARK1
        )

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
        self._text += text
        if len(self._chunks) > 32:
            self._chunks = [self._text]
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
    """Interjection — same grow-left bubble as a user turn."""

    DEFAULT_CSS = """
    InterjectionBlock {
        height: auto;
        width: 100%;
        padding: 0;
        margin: 0 2 1 0;
        border: none;
    }
    """

    def __init__(self, message: str, **kwargs):
        super().__init__(**kwargs)
        self._message = message
        self._ref = short_ref(message, prefix="bell") if "id=bell_" in (message or "") or "(id=" in (message or "") else ""
        import re
        m = re.search(r"id=(bell_[a-zA-Z0-9_]+)", message or "")
        if m:
            self._ref = short_ref(m.group(1))

    def on_resize(self) -> None:
        self.refresh()

    def render(self):
        mark, color = nick_mark("eric")
        inner = Text()
        inner.append(f"{mark} ", style=f"bold {color}")
        inner.append("interjection  ", style=f"bold {Theme.BR_ORANGE}")
        inner.append(self._message, style=Theme.BR_ORANGE)
        if self._ref:
            inner.append(f"  {self._ref}", style=Theme.DARK4)
        total = self.size.width if self.size.width >= 40 else 80
        return layout_painted_bubble(
            inner, is_human=True, total=total, border=color, fill=Theme.DARK2
        )


# =============================================================================
# Operator Identity Screen
# =============================================================================

