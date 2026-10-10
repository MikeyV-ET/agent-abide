#!/usr/bin/env python3
"""Minimal Textual/xterm.js repro: mixed row types + 1-cell scrollbar.

Keys: s solid, a ascii, v vkey, n none,
      d ascii-marks, e emoji, g gutter, l left-emoji, q quit.
"""
from __future__ import annotations

import argparse

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static
from textual.color import Color
from rich.text import Text
from pathlib import Path as _P
import sys as _sys
_sys.path.insert(0, str(_P(__file__).resolve().parent))
from chat_widgets import ToolRunStack, ToolCallPanel, short_ref
from theme import Theme


def painted_bubble(inner: str, *, is_human: bool, total: int, border: str, fill: str) -> Text:
    """Same recipe as nav_widgets.layout_painted_bubble: ╭─╮ and fill every cell."""
    from rich.console import Console
    total = total if total >= 40 else 80
    gutter = max(4, total * 15 // 100)
    edge = 1
    col = max(12, total - gutter - edge)
    inner_col = max(8, col - 2)
    console = Console(width=inner_col, force_terminal=True, color_system="truecolor")
    body = Text(inner)
    wrapped = [ln.copy() for ln in (body.wrap(console, inner_col) or [Text()])]
    content_w = min(inner_col, max(1, max((ln.cell_len for ln in wrapped), default=1)))
    box_w = content_w + 2
    left = max(0, total - box_w - edge) if is_human else edge
    fill_style = f"on {fill}"
    out = Text()
    out.append(" " * left)
    out.append("╭" + "─" * content_w + "╮", style=border)
    for ln in wrapped:
        out.append("\n")
        out.append(" " * left)
        out.append("│", style=border)
        piece = ln.copy()
        padn = max(0, content_w - piece.cell_len)
        piece.stylize(fill_style)
        out.append(piece)
        if padn:
            out.append(" " * padn, style=fill_style)
        out.append("│", style=border)
    out.append("\n")
    out.append(" " * left)
    out.append("╰" + "─" * content_w + "╯", style=border)
    return out



TOOL_LINES = [
    "Read health.json",
    "Read startup_history.json",
    "List doorbells",
    "List tui/inbox",
    "List localmail",
    "Read conversation.jsonl",
    "List interjections",
    "Execute python3 -c import",
]


class Line(Static):
    DEFAULT_CSS = "Line { height: 1; width: 100%; }"


class OnePanel(ToolCallPanel):
    """density=one with selectable marks for the extra-| isolation.

    ascii  — product path (super().render)
    emoji  — ✓ 📖 ▸ padded to full inner width (dirty control)
    gutter — same glyphs, last 2 inner cells always space
    left   — left emoji, ASCII '>' at the right, no reserve
    """

    EMOJI_KIND = {
        "read": "📖", "execute": "⚡", "edit": "✏️",
        "search": "🔍", "think": "💭", "other": "📋",
    }

    def __init__(self, *args, marks: str = "ascii", reserve: int = 0, **kwargs):
        super().__init__(*args, **kwargs)
        self.marks = marks
        self.reserve = reserve

    def render(self):
        if self.density != "one" or self.marks == "ascii":
            return super().render()
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
        cmd = (self.tool_command or self.tool_title or self.tool_kind or "tool").strip()
        cmd = " ".join(cmd.split())
        if len(cmd) > 56:
            cmd = cmd[:55] + "…"
        kicon = self.EMOJI_KIND.get(self.tool_kind, "🔧")
        ref = short_ref(self.tool_id)
        line = Text()
        line.append(f"{status_icon} ", style=f"bold {border_style}")
        line.append(f"{kicon} {cmd}", style=Theme.FG)
        if ref:
            line.append(f"  {ref}", style=Theme.DARK4)
        if self.marks == "left":
            line.append("  >", style=Theme.DARK4)
        else:
            line.append("  ▸", style=Theme.DARK4)
        width = int(self.size.width or 0)
        if width < 4:
            try:
                width = int(self.parent.size.width or 0)
            except Exception:
                width = 0
        if width < 4:
            width = 72
        reserve = self.reserve if self.marks == "gutter" else 0
        inner = max(4, width - reserve)
        if line.cell_len > inner:
            line.truncate(inner, overflow="crop")
        padn = inner - line.cell_len
        if padn > 0:
            line.append(" " * padn)
        if reserve > 0:
            line.append(" " * reserve)
        return line


class Gutter(Static):
    DEFAULT_CSS = "Gutter { width: 3; height: auto; }"


class AgentBubble(Static):
    DEFAULT_CSS = "AgentBubble { width: 100%; height: auto; margin: 1 0; }"

    def __init__(self, text: str) -> None:
        super().__init__()
        self._speech = text

    def render(self) -> Text:
        w = int(self.size.width or 80)
        return painted_bubble(self._speech, is_human=False, total=w, border="#7c3aed", fill="#f5f3ff")


class UserBubble(Static):
    DEFAULT_CSS = "UserBubble { width: 100%; height: auto; margin: 1 0; }"

    def __init__(self, text: str) -> None:
        super().__init__()
        self._speech = text

    def render(self) -> Text:
        w = int(self.size.width or 80)
        return painted_bubble(self._speech, is_human=True, total=w, border="#ea580c", fill="#fff7ed")


class ContinueLine(Static):
    DEFAULT_CSS = """
    ContinueLine {
        width: 100%;
        height: auto;
        margin: 1 1;
        color: #2563eb;
    }
    """


class ExpandedTool(Static):
    DEFAULT_CSS = """
    ExpandedTool {
        width: 100%;
        height: auto;
        margin: 0 1 1 1;
        padding: 0 1;
        color: #1a1a1e;
    }
    """

    def on_mount(self) -> None:
        self.styles.border = ("solid", Color.parse("#a1a1aa"))


class Stack(Horizontal):
    DEFAULT_CSS = """
    Stack {
        width: 100%;
        height: auto;
        margin: 1 1 1 1;
        background: #f7f7f8;
    }
    Stack > .body { width: 1fr; height: auto; }
    """

    def compose(self) -> ComposeResult:
        yield Gutter("  8")
        with Vertical(classes="body"):
            for i, title in enumerate(TOOL_LINES):
                prefix = "▸ " if i == 0 else "  "
                yield Line(f"{prefix}✓  {title}  id:demo{i}  ▸")

    def apply_border(self, kind: str) -> None:
        if kind == "none":
            self.styles.border = "none"
        else:
            self.styles.border = (kind, Color.parse("#a1a1aa"))


class Repro(App):
    CSS = """
    Screen { background: #f7f7f8; color: #1a1a1e; }
    #banner { height: 1; color: #71717a; }
    VerticalScroll {
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
        height: 1fr;
        scrollbar-background: #f7f7f8;
        scrollbar-color: #3b6ea5;
    }
    """
    BINDINGS = [
        ("s", "border('solid')", "solid"),
        ("a", "border('ascii')", "ascii"),
        ("v", "border('vkey')", "vkey"),
        ("n", "border('none')", "none"),
        ("d", "marks('ascii')", "ascii-marks"),
        ("e", "marks('emoji')", "emoji"),
        ("g", "marks('gutter')", "gutter"),
        ("l", "marks('left')", "left-emoji"),
        ("q", "quit", "quit"),
    ]

    def __init__(self, border: str = "solid", marks: str = "ascii") -> None:
        super().__init__()
        self.border_kind = border
        self.marks_kind = marks

    def compose(self) -> ComposeResult:
        yield Static("", id="banner")
        with VerticalScroll():
            yield ContinueLine(
                "T4 continue ────  [continue id=demo] Your turn ended."
            )
            yield AgentBubble("Overnight continue. I'll check whether this is a restart.")
            yield ToolRunStack()
            yield AgentBubble("Morning restart. Inboxes empty. Standing by.")
            yield UserBubble("@ eric (tui) Smoke test: what is 20 times 19?")
            yield ExpandedTool(
                "▶ Execute python3 …\n"
                "  click to expand\n"
                "  output: 380"
            )
            # keep the view overflowing so the 1-cell scrollbar stays
            for i in range(24):
                yield Line(f"  pad line {i}")

    def on_mount(self) -> None:
        stack = self.query_one(ToolRunStack)

        for i, title in enumerate(TOOL_LINES):
            pan = OnePanel(f"demo{i}", title, kind="read", marks=self.marks_kind, reserve=2)
            pan.density = "one"
            pan.set_status("completed")
            pan.tool_command = title
            stack.add_panel(pan)
        stack.styles.width = "100%"
        self._apply_stack_border(self.border_kind)
        self._paint_banner()

    def _apply_stack_border(self, kind: str) -> None:
        # Knob 1: clone was clean with border on the OUTER horizontal.
        stack = self.query_one(ToolRunStack)
        try:
            frame = stack.query_one(".tool-run-frame")
            frame.styles.border = "none"
        except Exception:
            pass
        if kind == "none":
            stack.styles.border = "none"
        else:
            from textual.color import Color as C
            stack.styles.border = (kind, C.parse("#a1a1aa"))

    def _paint_banner(self) -> None:
        self.query_one("#banner").update(
            f"repro_stack_border  stack={self.border_kind} marks={self.marks_kind}  "
            f"keys: s/a/v/n border  d=ascii e=emoji g=gutter l=left q=quit"
        )

    def action_border(self, kind: str) -> None:
        self.border_kind = kind
        self._apply_stack_border(kind)
        self._paint_banner()

    def action_marks(self, kind: str) -> None:
        self.marks_kind = kind
        for pan in self.query(OnePanel):
            pan.marks = kind
            pan.reserve = 2 if kind == "gutter" else 0
            pan.refresh()
        self._paint_banner()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--border", default="solid", choices=("solid", "ascii", "vkey", "none"))
    p.add_argument("--marks", default="ascii", choices=("ascii", "emoji", "gutter", "left"))
    args = p.parse_args()
    Repro(border=args.border, marks=args.marks).run()


if __name__ == "__main__":
    main()
