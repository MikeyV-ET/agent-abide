#!/usr/bin/env python3
"""Minimal Textual/xterm.js repro: mixed row types + 1-cell scrollbar.

Keys: s solid, a ascii, v vkey, n none, q quit.
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
from chat_widgets import ToolRunStack, ToolCallPanel


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
    }
    """
    BINDINGS = [
        ("s", "border('solid')", "solid"),
        ("a", "border('ascii')", "ascii"),
        ("v", "border('vkey')", "vkey"),
        ("n", "border('none')", "none"),
        ("q", "quit", "quit"),
    ]

    def __init__(self, border: str = "solid") -> None:
        super().__init__()
        self.border_kind = border

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

        class NoPadPanel(ToolCallPanel):
            def render(self):
                from theme import Theme
                from chat_widgets import short_ref
                status_icon, border_style = "✓", Theme.BR_GREEN
                try:
                    self.styles.border = "none"
                except Exception:
                    pass
                self.styles.padding = (0, 0)
                self.styles.margin = (0, 0, 0, 0)
                try:
                    self.styles.height = 1
                except Exception:
                    pass
                cmd = (self.tool_title or "tool").strip()
                if len(cmd) > 56:
                    cmd = cmd[:55] + "…"
                kicon = {"read": "📖", "execute": "⚡"}.get(self.tool_kind, "🔧")
                line = Text()
                line.append(f"{status_icon} ", style=f"bold {border_style}")
                line.append(f"{kicon} {cmd}", style=Theme.FG)
                line.append(f"  {short_ref(self.tool_id)}  ▸", style=Theme.DARK4)
                return line  # no pad-to-width

        for i, title in enumerate(TOOL_LINES):
            pan = NoPadPanel(f"demo{i}", title, kind="read")
            pan.density = "one"
            pan.set_status("completed")
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
            f"repro_stack_border  stack={self.border_kind}  "
            f"keys: s=solid a=ascii v=vkey n=none q=quit"
        )

    def action_border(self, kind: str) -> None:
        self.border_kind = kind
        self._apply_stack_border(kind)
        self._paint_banner()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--border", default="solid", choices=("solid", "ascii", "vkey", "none"))
    args = p.parse_args()
    Repro(border=args.border).run()


if __name__ == "__main__":
    main()
