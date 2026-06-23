"""TUI paste behavior tests.

Bug: Pasting multi-line text in normal mode fires Enter-as-send on every newline,
producing N separate Submitted events instead of inserting the text as one block.

Tests that MessageInput._on_paste intercepts multi-line paste and inserts the text
without triggering send. Uses Textual's Paste event (bracketed paste mode).

Run: pytest tests/test_tui_paste.py -v
"""

import asyncio
import pytest
from textual.app import App, ComposeResult
from textual.events import Paste

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tui'))
from asdaaas_tui import MessageInput


class PasteTestApp(App):
    """Minimal app that hosts a MessageInput and records Submitted events."""

    def __init__(self):
        super().__init__()
        self.submitted_messages: list[str] = []

    def compose(self) -> ComposeResult:
        yield MessageInput(id="msg-input")

    def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
        self.submitted_messages.append(event.text)


MULTI_LINE_TEXT = "line one\nline two\nline three"
SINGLE_LINE_TEXT = "just one line"


@pytest.mark.asyncio
async def test_multiline_paste_does_not_fire_submitted():
    """Pasting multi-line text should NOT produce Submitted events.
    It should insert the text into the input area."""
    async with PasteTestApp().run_test() as pilot:
        app = pilot.app
        msg_input = app.query_one("#msg-input", MessageInput)

        # Simulate bracketed paste event
        await msg_input._on_paste(Paste(MULTI_LINE_TEXT))
        await pilot.pause()

        assert len(app.submitted_messages) == 0, (
            f"Paste should not trigger send, but got {len(app.submitted_messages)} "
            f"Submitted events: {app.submitted_messages}"
        )
        # Text should be in the input area
        assert "line one" in msg_input.text
        assert "line two" in msg_input.text
        assert "line three" in msg_input.text


@pytest.mark.asyncio
async def test_multiline_paste_switches_to_edit_mode():
    """After pasting multi-line text, input should be in edit mode
    so Enter inserts newlines instead of sending."""
    async with PasteTestApp().run_test() as pilot:
        app = pilot.app
        msg_input = app.query_one("#msg-input", MessageInput)

        # Start in normal mode
        assert not msg_input.multiline_mode

        # Paste multi-line text
        await msg_input._on_paste(Paste(MULTI_LINE_TEXT))
        await pilot.pause()

        assert msg_input.multiline_mode, (
            "Multi-line paste should auto-switch to edit mode"
        )


@pytest.mark.asyncio
async def test_single_line_paste_stays_in_normal_mode():
    """Pasting single-line text should NOT switch to edit mode."""
    async with PasteTestApp().run_test() as pilot:
        app = pilot.app
        msg_input = app.query_one("#msg-input", MessageInput)

        assert not msg_input.multiline_mode

        await msg_input._on_paste(Paste(SINGLE_LINE_TEXT))
        await pilot.pause()

        assert not msg_input.multiline_mode, (
            "Single-line paste should stay in normal mode"
        )


@pytest.mark.asyncio
async def test_single_line_paste_inserts_text():
    """Pasting single-line text should insert it normally."""
    async with PasteTestApp().run_test() as pilot:
        app = pilot.app
        msg_input = app.query_one("#msg-input", MessageInput)

        await msg_input._on_paste(Paste(SINGLE_LINE_TEXT))
        await pilot.pause()

        assert msg_input.text == SINGLE_LINE_TEXT


@pytest.mark.asyncio
async def test_paste_not_duplicated():
    """Pasting text should insert it exactly once, not twice.

    Bug: Textual's message dispatch walks the MRO and calls _on_paste
    on each class. Without prevent_default(), both MessageInput._on_paste
    AND TextArea._on_paste fire, doubling the pasted text.

    This test uses post_message to send a Paste event through the real
    dispatch chain (unlike the tests above that call _on_paste directly).
    """
    async with PasteTestApp().run_test() as pilot:
        app = pilot.app
        msg_input = app.query_one("#msg-input", MessageInput)

        msg_input.post_message(Paste(SINGLE_LINE_TEXT))
        await pilot.pause()
        await pilot.pause()

        assert msg_input.text == SINGLE_LINE_TEXT, (
            f"Paste should insert text exactly once. "
            f"Got: {msg_input.text!r} (length {len(msg_input.text)}) "
            f"Expected: {SINGLE_LINE_TEXT!r} (length {len(SINGLE_LINE_TEXT)})"
        )


@pytest.mark.asyncio
async def test_normal_enter_still_sends():
    """In normal mode, pressing Enter should still send (regression guard)."""
    async with PasteTestApp().run_test() as pilot:
        app = pilot.app
        msg_input = app.query_one("#msg-input", MessageInput)

        # Type some text and press Enter
        msg_input.insert("hello world")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert len(app.submitted_messages) == 1
        assert app.submitted_messages[0] == "hello world"
