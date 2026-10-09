#!/usr/bin/env python3
"""Minimal Textual border repro for xterm.js / Electron.

One 80%-wide Vertical, eight one-line rows, a border. Nothing else.
Keys: s solid, a ascii, v vkey, n none, q quit.
"""
from __future__ import annotations

import argparse

from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static
from textual.color import Color


LINES = [
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


class Stack(Vertical):
    DEFAULT_CSS = """
    Stack {
        width: 100%;
        height: auto;
        margin: 1 1 1 1;
        background: #f7f7f8;
    }
    """

    def compose(self) -> ComposeResult:
        for i, title in enumerate(LINES):
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
            yield Stack()

    def on_mount(self) -> None:
        self.query_one(Stack).apply_border(self.border_kind)
        self._paint_banner()

    def _paint_banner(self) -> None:
        self.query_one("#banner").update(
            f"repro_stack_border  border={self.border_kind}  "
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
