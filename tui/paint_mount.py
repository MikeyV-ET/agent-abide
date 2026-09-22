"""Build Textual widgets from ChatState paint units (display model → glass)."""
from __future__ import annotations

from typing import Any, Optional

from chat_model import (
    SpeechItem,
    ThinkingItem,
    ToolItem,
    SystemItem,
    TurnMark,
)
from paint_fold import DisplayPolicy, policy_for_item


def widget_for_item(item: Any):
    """Return a new widget for one paint unit, or None to skip."""
    from chat_widgets import (
        UserMessage,
        AgentMessage,
        ThinkingBlock,
        ToolCallPanel,
    )
    from chrome_widgets import TurnSeparator, SystemAlert

    pol = policy_for_item(item)
    if pol is DisplayPolicy.DROP:
        return None

    if isinstance(item, TurnMark):
        return TurnSeparator(item.number, item.trigger or "", item.ts or "")

    if isinstance(item, SpeechItem):
        if item.kind == "user":
            return UserMessage(item.text or "")
        w = AgentMessage()
        if item.text:
            w.append_chunk(item.text)
        return w

    if isinstance(item, ThinkingItem):
        w = ThinkingBlock()
        if item.text:
            w.append_chunk(item.text)
        return w

    if isinstance(item, ToolItem):
        # ToolCallPanel(tool_id, title, kind="", ts="")
        try:
            w = ToolCallPanel(
                item.tool_id or "tool",
                item.title or "tool",
                item.kind or "",
            )
        except TypeError:
            w = ToolCallPanel(item.tool_id or "tool", item.title or "tool")
        status = item.status or "running"
        # map completed
        if status in ("completed", "failed", "in_progress", "running", "pending"):
            try:
                # ToolCallPanel may use tool_status field
                if hasattr(w, "set_status"):
                    w.set_status(
                        "completed"
                        if status == "completed"
                        else "failed"
                        if status == "failed"
                        else "in_progress"
                        if status in ("running", "in_progress", "pending")
                        else status
                    )
                else:
                    w.tool_status = (
                        "completed"
                        if status == "completed"
                        else "failed"
                        if status == "failed"
                        else "in_progress"
                    )
            except Exception:
                w.tool_status = "completed" if status == "completed" else "in_progress"
        if item.output:
            if hasattr(w, "set_output"):
                try:
                    w.set_output(item.output)
                except Exception:
                    w.tool_output = item.output
            else:
                w.tool_output = item.output
        if hasattr(w, "_collapsed"):
            w._collapsed = bool(getattr(item, "collapsed", True))
        return w

    if isinstance(item, SystemItem):
        sev = "warning" if item.kind in ("retry_state", "doom_loop_detected") else "information"
        # SystemAlert severity names
        try:
            return SystemAlert(item.text or item.kind, severity=sev)
        except TypeError:
            return SystemAlert(item.text or item.kind)

    return None


def mount_items(content_scroll, items: list, *, before: Optional[Any] = None) -> int:
    """Mount paint units onto a VerticalScroll. Returns widgets mounted."""
    n = 0
    for item in items:
        w = widget_for_item(item)
        if w is None:
            continue
        if before is not None:
            content_scroll.mount(w, before=before)
        else:
            content_scroll.mount(w)
        n += 1
    return n
