"""Smoke: widget render paths used live (catches missing imports after extract)."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))

from rich.markdown import Markdown as RichMarkdown
from chat_widgets import _flatten_to_text, AgentMessage, ThinkingBlock, ToolCallPanel


def test_flatten_markdown():
    t = _flatten_to_text(RichMarkdown("**hello** and `code`"), width=60)
    assert "hello" in t.plain


def test_agent_message_render():
    m = AgentMessage()
    m.append_chunk("**hi** from agent\n\n- item")
    r = m.render()
    assert hasattr(r, "plain")
    assert len(r.plain) > 0


def test_thinking_render():
    b = ThinkingBlock()
    b.append_chunk("reasoning...")
    r = b.render()
    assert "reasoning" in r.plain


def test_tool_panel_snippet_render():
    p = ToolCallPanel("id", "bash")
    p.set_output("\n".join(f"line{i}" for i in range(10)))
    r = p.render()
    # collapsed snippet mode — Text or similar
    assert r is not None


def test_asdaaas_tui_imports():
    """Full module import — workers not started without App run."""
    import asdaaas_tui
    from asdaaas_tui import AsdaaasTUI, MessageInput, ToolCallPanel
    assert AsdaaasTUI is not None
    assert MessageInput is not None
