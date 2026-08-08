"""Message input bar for asdaaas TUI."""
from __future__ import annotations

from textual.widgets import TextArea
from textual.events import Paste

from theme import Theme


class MessageInput(TextArea):
    """Multiline chat input. Enter sends; Ctrl+Enter / Shift+Enter / ^J insert newline.

    No separate EDIT mode — one key map always.
    """

    DEFAULT_CSS = """
    MessageInput {
        height: auto;
        min-height: 4;
        border: heavy #504945;
        padding: 0 1;
    }
    MessageInput:focus {
        border: heavy #7c6f64;
    }
    """

    class Submitted(TextArea.Changed):
        """Fired when user presses Enter (without Shift/Ctrl)."""
        def __init__(self, text_area: "MessageInput", text: str):
            super().__init__(text_area)
            self.text = text

    def __init__(self, placeholder: str = "", **kwargs):
        super().__init__("", language=None, show_line_numbers=False, **kwargs)
        self._placeholder = placeholder
        self._history: list[str] = []
        self._history_index: int = -1
        self._draft: str = ""  # Saves current input when browsing history
        # Register underscore cursor theme
        from textual.widgets.text_area import TextAreaTheme
        from rich.style import Style
        underscore_theme = TextAreaTheme(
            name="underscore",
            cursor_style=Style(underline=True),
            cursor_line_style=Style(),
        )
        self.register_theme(underscore_theme)
        self.theme = "underscore"
        self.border_title = ""
        self.border_subtitle = ""

    # Soft guidance only — Textual has no hard paste cap; terminals often do (~8KB–1MB).
    PASTE_WARN_CHARS = 50_000
    # Absolute ceiling; actual cap is min(this, ~half terminal) so chat stays visible.
    MAX_INPUT_HEIGHT = 20

    def _max_height_cells(self) -> int:
        """Max input height in rows: leave ~half the screen for chat + 1 for footer."""
        try:
            screen_h = self.app.size.height
            return max(4, min(self.MAX_INPUT_HEIGHT, (screen_h // 2) - 1))
        except Exception:
            return min(self.MAX_INPUT_HEIGHT, 10)

    def _set_height_for_content(self, min_lines: int = 1) -> None:
        """Grow/shrink height with content, never past _max_height_cells."""
        try:
            visual = max(self.virtual_size.height, min_lines, 1)
            cap = self._max_height_cells()
            self.styles.height = max(2, min(visual + 2, cap))
        except Exception:
            pass

    async def _on_paste(self, event) -> None:
        """Handle bracketed paste: insert text as one block (no send, no mode switch).

        No TUI-imposed character limit. Failures usually come from:
        - Terminal/WSL truncating the bracketed-paste stream
        - Paste containing ESC sequences that end bracketed mode early (Textual)
        - Input not focused / copy-mode (F7) eating selection paste
        """
        if self.read_only:
            return
        text = event.text or ""
        if not text:
            try:
                self.app.notify("Paste was empty (terminal may have truncated)", severity="warning")
            except Exception:
                pass
            event.prevent_default()
            event.stop()
            return
        # Normalize CRLF from Windows paste
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        n = len(text)
        lines = text.count("\n") + (1 if text else 0)
        try:
            self.insert(text)
        except Exception as e:
            try:
                self.app.notify(f"Paste insert failed: {e}", severity="error")
            except Exception:
                pass
            event.prevent_default()
            event.stop()
            return
        self.focus()
        self._set_height_for_content(min_lines=lines)
        try:
            if n >= self.PASTE_WARN_CHARS:
                self.app.notify(
                    f"Pasted {n:,} chars / ~{lines} lines (large — if truncated, use a file path)",
                    severity="warning",
                )
            else:
                self.app.notify(f"Pasted {n:,} chars", severity="information", timeout=2)
        except Exception:
            pass
        event.prevent_default()
        event.stop()

    def _get_wrap_width(self) -> int:
        """Get the actual character width available for text wrapping."""
        try:
            region = self.scrollable_content_region
            return max(region.width, 1)
        except Exception:
            return max(self.size.width - 4, 1)

    def _is_multiline(self) -> bool:
        """Check if input has multiple visual lines (newlines or wrapping)."""
        if "\n" in self.text:
            return True
        return len(self.text) > self._get_wrap_width()

    def undo(self) -> None:
        """Override undo to handle Textual cursor desync bug.

        Textual's _undo_batch calls _refresh_size() before updating the cursor.
        With auto-height, the scrollbar refresh tries to scroll to a cursor
        position that references lines removed by the undo.
        """
        try:
            super().undo()
        except ValueError:
            line_count = self.document.line_count
            row = min(self.cursor_location[0], max(0, line_count - 1))
            last_line = self.document.get_line(row)
            col = min(self.cursor_location[1], len(last_line))
            self.move_cursor((row, col))
            try:
                self._refresh_size()
            except ValueError:
                pass

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Recalculate height when text changes, using TextArea's own virtual size."""
        def _update_height():
            self._set_height_for_content()
            try:
                if self.virtual_size.height > 8:
                    self.scroll_cursor_visible()
            except Exception:
                pass
        self.call_after_refresh(_update_height)

    def _on_key(self, event) -> None:
        """Enter sends; Ctrl+Enter / Shift+Enter / Ctrl+J insert newline."""
        # Pass Home/End/PageUp/PageDown to the app for scroll/history actions
        if event.key in ("f3", "f5", "f6"):
            return  # Let these bubble to app-level bindings
        if event.key in ("home", "end", "pageup", "pagedown"):
            event.prevent_default()
            event.stop()
            if event.key == "home":
                self.app.action_scroll_top()
            elif event.key == "end":
                self.app.action_scroll_bottom()
            elif event.key == "pageup":
                self.app.action_load_history()
            elif event.key == "pagedown":
                try:
                    scroll = self.app._content_scroll()
                    scroll.scroll_page_down(animate=False)
                except Exception:
                    pass
            return

        newline_keys = ("shift+enter", "ctrl+enter", "ctrl+j")
        if event.key in newline_keys:
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return

        if event.key == "enter":
            # If slash menu is visible, select the highlighted option
            try:
                slash_menu = self.app.query_one("#slash-menu")
                if slash_menu.display and slash_menu.highlighted is not None:
                    event.prevent_default()
                    event.stop()
                    slash_menu.action_select()
                    return
            except Exception:
                pass
            event.prevent_default()
            event.stop()
            text = self.text.strip()
            if text:
                self._history.append(text)
                self._history_index = -1
                self._draft = ""
                self.post_message(self.Submitted(self, text))
                self.clear()
                self._set_height_for_content(min_lines=1)
            return

        if event.key in ("up", "down") and self._is_multiline():
            # Multiline: move cursor within text, prevent bubbling to parent scroll
            event.prevent_default()
            event.stop()
            if event.key == "up":
                self.action_cursor_up()
            else:
                self.action_cursor_down()
            return

        if event.key == "up":
            # If slash menu is visible, navigate it
            try:
                slash_menu = self.app.query_one("#slash-menu")
                if slash_menu.display:
                    event.prevent_default()
                    event.stop()
                    slash_menu.action_cursor_up()
                    return
            except Exception:
                pass
            # Only use history nav when input is single-line
            event.prevent_default()
            event.stop()
            if self._history:
                if self._history_index == -1:
                    self._draft = self.text
                    self._history_index = len(self._history) - 1
                elif self._history_index > 0:
                    self._history_index -= 1
                self.clear()
                self.insert(self._history[self._history_index])
            return

        if event.key == "down":
            # If slash menu is visible, navigate it
            try:
                slash_menu = self.app.query_one("#slash-menu")
                if slash_menu.display:
                    event.prevent_default()
                    event.stop()
                    slash_menu.action_cursor_down()
                    return
            except Exception:
                pass
            event.prevent_default()
            event.stop()
            if self._history_index >= 0:
                if self._history_index < len(self._history) - 1:
                    self._history_index += 1
                    self.clear()
                    self.insert(self._history[self._history_index])
                else:
                    self._history_index = -1
                    self.clear()
                    self.insert(self._draft)
            return
