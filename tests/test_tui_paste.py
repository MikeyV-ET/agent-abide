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
async def test_multiline_paste_enter_still_sends():
    """No EDIT mode: after multi-line paste, Enter still sends (Ctrl+Enter = newline)."""
    async with PasteTestApp().run_test() as pilot:
        app = pilot.app
        msg_input = app.query_one("#msg-input", MessageInput)

        await msg_input._on_paste(Paste(MULTI_LINE_TEXT))
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()

        assert len(app.submitted_messages) == 1
        assert "line one" in app.submitted_messages[0]
        assert "line three" in app.submitted_messages[0]


@pytest.mark.asyncio
async def test_ctrl_enter_inserts_newline():
    """Ctrl+Enter inserts a newline without sending."""
    async with PasteTestApp().run_test() as pilot:
        app = pilot.app
        msg_input = app.query_one("#msg-input", MessageInput)

        msg_input.insert("hello")
        await pilot.pause()
        await pilot.press("ctrl+enter")
        await pilot.pause()
        msg_input.insert("world")
        await pilot.pause()

        assert len(app.submitted_messages) == 0
        assert "hello\nworld" in msg_input.text or msg_input.text == "hello\nworld"


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


BORING_COMPANY_TEXT = (
    "The Boring Company is an American infrastructure and tunnel construction\n"
    "services company founded by Elon Musk in December 2016.\n"
    "The company has constructed an underground transportation system."
)


@pytest.mark.asyncio
async def test_paste_undo_does_not_trigger_submit():
    """Bug: Paste 3x multiline, ctrl-z 3x to undo. First 2 undos work,
    3rd undo triggers original paste bug (send-per-line fault).

    Issues 0021/0025. Undo may replay individual events bypassing
    the paste detection in _on_paste.
    """
    async with PasteTestApp().run_test() as pilot:
        app = pilot.app
        msg_input = app.query_one("#msg-input", MessageInput)

        # Paste 3x
        for _ in range(3):
            await msg_input._on_paste(Paste(BORING_COMPANY_TEXT))
            await pilot.pause()

        # Should have text but no submits
        assert len(app.submitted_messages) == 0
        assert len(msg_input.text) > 0

        # Undo 3x — none should trigger Submitted
        for i in range(3):
            await pilot.press("ctrl+z")
            await pilot.pause()
            assert len(app.submitted_messages) == 0, (
                f"Undo #{i+1} triggered {len(app.submitted_messages)} "
                f"Submitted event(s): {app.submitted_messages}"
            )

        # After 3 undos, text should be empty (all pastes reversed)
        assert msg_input.text == "", (
            f"After 3 undos, text should be empty but got: {msg_input.text!r}"
        )


@pytest.mark.asyncio
async def test_paste_undo_redo_cycle():
    """Paste, undo, paste again — no submits should fire at any point."""
    async with PasteTestApp().run_test() as pilot:
        app = pilot.app
        msg_input = app.query_one("#msg-input", MessageInput)

        # Paste
        await msg_input._on_paste(Paste(BORING_COMPANY_TEXT))
        await pilot.pause()
        assert len(app.submitted_messages) == 0

        # Undo
        await pilot.press("ctrl+z")
        await pilot.pause()
        assert len(app.submitted_messages) == 0

        # Paste again
        await msg_input._on_paste(Paste(BORING_COMPANY_TEXT))
        await pilot.pause()
        assert len(app.submitted_messages) == 0

        # Undo again
        await pilot.press("ctrl+z")
        await pilot.pause()
        assert len(app.submitted_messages) == 0, (
            f"Paste-undo-paste-undo cycle triggered submit: {app.submitted_messages}"
        )


@pytest.mark.asyncio
async def test_multiline_paste_stays_inside_bottom_bar():
    """Regression: large multiline paste must not overflow #bottom-bar.

    Bug (2026-08-07): MessageInput grew past bottom-bar max-height:12 and
    painted outside the dock — looked like an "escaped text window".
    """
    from textual.containers import Vertical

    class LayoutPasteApp(App):
        CSS = """
        #bottom-bar {
            dock: bottom;
            height: auto;
            max-height: 50%;
        }
        """
        def compose(self) -> ComposeResult:
            yield Vertical(id="main")
            with Vertical(id="bottom-bar"):
                yield MessageInput(id="msg-input")

        def on_message_input_submitted(self, event: MessageInput.Submitted) -> None:
            pass

    big = "\n".join(f"line {i}" for i in range(40))
    async with LayoutPasteApp().run_test(size=(80, 40)) as pilot:
        app = pilot.app
        msg_input = app.query_one("#msg-input", MessageInput)
        bar = app.query_one("#bottom-bar")

        await msg_input._on_paste(Paste(big))
        await pilot.pause()
        await pilot.pause()

        # Input must not be taller than its docked parent
        assert msg_input.size.height <= bar.size.height, (
            f"input h={msg_input.size.height} escaped bar h={bar.size.height}"
        )
        # Parent must not consume the whole screen
        assert bar.size.height <= 20, (
            f"bottom-bar h={bar.size.height} ate the screen (terminal=40)"
        )
        assert "line 0" in msg_input.text
        assert "line 39" in msg_input.text


@pytest.mark.asyncio
async def test_multiline_paste_then_enter_sends_once():
    """Paste multi-line then Enter: one submit, not N (no EDIT mode needed)."""
    async with PasteTestApp().run_test() as pilot:
        app = pilot.app
        msg_input = app.query_one("#msg-input", MessageInput)

        await msg_input._on_paste(Paste(MULTI_LINE_TEXT))
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()

        assert len(app.submitted_messages) == 1
        assert app.submitted_messages[0].count("line") >= 3
