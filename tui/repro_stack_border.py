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
    DEFAULT_CSS = """
    AgentBubble {
        width: 70%;
        height: auto;
        margin: 1 8 1 1;
        padding: 0 1;
        color: #1a1a1e;
        background: #f5f3ff;
    }
    """

    def on_mount(self) -> None:
        self.styles.border = ("round", Color.parse("#7c3aed"))


class UserBubble(Static):
    DEFAULT_CSS = """
    UserBubble {
        width: 70%;
        height: auto;
        margin: 1 2 1 8;
        padding: 0 1;
        align: right middle;
        color: #1a1a1e;
        background: #fff7ed;
    }
    """

    def on_mount(self) -> None:
        self.styles.border = ("round", Color.parse("#ea580c"))


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
            yield Stack()
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
        self.query_one(Stack).apply_border(self.border_kind)
        self._paint_banner()

    def _paint_banner(self) -> None:
        self.query_one("#banner").update(
            f"repro_stack_border  stack={self.border_kind}  "
            f"keys: s=solid a=ascii v=vkey n=none q=quit"
        )

    def action_border(self, kind: str) -> None:
        self.border_kind = kind
        self.query_one(Stack).apply_border(kind)
        self._paint_banner()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--border", default="solid", choices=("solid", "ascii", "vkey", "none"))
    args = p.parse_args()
    Repro(border=args.border).run()


if __name__ == "__main__":
    main()
