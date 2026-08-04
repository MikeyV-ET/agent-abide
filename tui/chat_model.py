"""Pure chat session model for asdaaas TUI.

Event → state without Textual. App layer mounts widgets from this state
(or continues to apply incrementally). Unit-testable with Squiggy-shaped events.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
import re

from chat_events import session_update, update_payload, chunk_text, tool_call_id


# ── Item types (scrollback model) ───────────────────────────────────────

@dataclass
class SpeechItem:
    text: str = ""
    kind: str = "agent"  # agent | user


@dataclass
class ThinkingItem:
    text: str = ""


@dataclass
class ToolItem:
    tool_id: str
    title: str = ""
    kind: str = ""
    status: str = "running"
    output: str = ""
    collapsed: bool = True  # display policy: snippet by default


@dataclass
class SystemItem:
    text: str
    kind: str = "hook"  # hook | compact | task | alert


@dataclass
class TurnMark:
    number: int
    trigger: str = ""
    ts: str = ""


@dataclass
class ChatState:
    """Per-agent chat scrollback model."""
    items: list[Any] = field(default_factory=list)
    logical_turn: int = 0
    # open streaming refs (indices into items)
    open_speech_idx: Optional[int] = None
    open_thinking_idx: Optional[int] = None
    tools: dict[str, int] = field(default_factory=dict)  # tool_id -> item index
    last_event_ts: Any = None

    def close_open_streams(self) -> None:
        self.open_speech_idx = None
        self.open_thinking_idx = None


def extract_interjections(text: str) -> tuple[str, list[str]]:
    """Extract <interjection> blocks. Returns (clean_text, messages)."""
    if "<interjection>" not in text:
        return text, []
    messages = []
    for m in re.finditer(r"<interjection>\n?(.*?)</interjection>\n?", text, re.DOTALL):
        body = m.group(1).strip()
        if body:
            lines = body.split("\n")
            if lines and lines[0].startswith("[system:"):
                body = "\n".join(lines[1:]).strip()
            messages.append(body)
    clean = re.sub(r"<interjection>\n?(.*?)</interjection>\n?", "", text, flags=re.DOTALL)
    return clean, messages


def interjection_key(message: str) -> str:
    """Stable dedup key: prefer doorbell id=…, else full stripped text."""
    m = re.search(r"\(id=([a-zA-Z0-9_]+)", message or "")
    if m:
        return f"bell:{m.group(1)}"
    return (message or "").strip()


def classify_turn_trigger(text: str) -> str:
    """Mirror asdaaas_tui.classify_turn_trigger for pure tests."""
    t = text or ""
    low = t.lower()
    if "localmail" in low[:60] or "[FROM:" in t[:30]:
        return "localmail"
    if "continue" in low[:80] and "your turn" in low:
        return "continue"
    if "doorbell" in low[:80] or low.startswith("[continue"):
        return "doorbell"
    if t.strip().startswith("/") or "operator" in low[:40]:
        return "operator"
    return "user"


# ── Reducer ─────────────────────────────────────────────────────────────

def apply_event(state: ChatState, event: dict) -> list[str]:
    """Apply one updates.jsonl event. Returns list of change tags for tests/UI.

    Does not coalesce — call coalesce_events on batches first if desired.
    """
    changes: list[str] = []
    su = session_update(event)
    update = update_payload(event)
    if event.get("timestamp") is not None:
        state.last_event_ts = event.get("timestamp")

    if su == "agent_message_chunk":
        text = chunk_text(event)
        if not text:
            return changes
        if state.open_speech_idx is None:
            state.items.append(SpeechItem(text=text, kind="agent"))
            state.open_speech_idx = len(state.items) - 1
            changes.append("speech_open")
        else:
            item = state.items[state.open_speech_idx]
            assert isinstance(item, SpeechItem)
            item.text += text
            changes.append("speech_append")
        return changes

    if su == "agent_thought_chunk":
        text = chunk_text(event)
        if not text:
            return changes
        if state.open_thinking_idx is None:
            state.items.append(ThinkingItem(text=text))
            state.open_thinking_idx = len(state.items) - 1
            changes.append("think_open")
        else:
            item = state.items[state.open_thinking_idx]
            assert isinstance(item, ThinkingItem)
            item.text += text
            changes.append("think_append")
        return changes

    if su == "tool_call":
        state.close_open_streams()
        tid = tool_call_id(event) or update.get("toolCallId", "")
        title = update.get("title", "unknown tool")
        kind = update.get("kind", "")
        item = ToolItem(tool_id=tid, title=title, kind=kind, collapsed=True)
        state.items.append(item)
        if tid:
            state.tools[tid] = len(state.items) - 1
        changes.append("tool_open")
        return changes

    if su == "tool_call_update":
        tid = tool_call_id(event)
        idx = state.tools.get(tid)
        if idx is None:
            title = update.get("title") or f"tool {(tid or '?')[:8]}"
            item = ToolItem(tool_id=tid, title=title, kind=update.get("kind", ""), collapsed=True)
            state.items.append(item)
            idx = len(state.items) - 1
            if tid:
                state.tools[tid] = idx
            changes.append("tool_open_late")
        item = state.items[idx]
        assert isinstance(item, ToolItem)
        if update.get("kind"):
            item.kind = update["kind"]
        if update.get("title"):
            item.title = update["title"]
        if update.get("status"):
            item.status = update["status"]
            if item.status in ("completed", "failed"):
                item.collapsed = True
        for c in update.get("content") or []:
            if c.get("type") == "content":
                inner = c.get("content") or {}
                text = inner.get("text") or ""
                if text:
                    clean, _inter = extract_interjections(text)
                    item.output = clean  # latest-wins style full replace
            elif c.get("type") == "diff":
                item.output = f"[diff] {c.get('path', '')}"
        changes.append("tool_update")
        return changes

    if su == "user_message_chunk":
        text = chunk_text(event)
        if not text:
            return changes
        state.close_open_streams()
        # Harness chrome — not a logical user turn
        if text.lstrip().startswith("<system-reminder>") or text.lstrip().startswith("<system_reminder>"):
            state.items.append(SystemItem(text=text, kind="system_reminder"))
            changes.append("system_reminder")
            return changes
        state.logical_turn += 1
        state.items.append(TurnMark(number=state.logical_turn, trigger=classify_turn_trigger(text)))
        state.items.append(SpeechItem(text=text, kind="user"))
        changes.append("user")
        return changes

    if su == "plan":
        # Represent as system note; full plan structure optional later
        n = len(update.get("entries") or [])
        state.items.append(SystemItem(text=f"Plan update ({n} entries)", kind="plan"))
        changes.append("plan")
        return changes

    if su in ("hook_annotation", "task_backgrounded", "task_completed",
              "auto_compact_started", "auto_compact_completed",
              "retry_state", "doom_loop_detected"):
        msg = update.get("message") or su
        if su == "task_backgrounded":
            msg = f"Task backgrounded: {update.get('command', '?')}"
        elif su == "task_completed":
            snap = update.get("task_snapshot") or {}
            msg = f"Task completed: {snap.get('command', '?')} exit={snap.get('exit_code', '?')}"
        elif su == "retry_state":
            msg = f"Retry: {update.get('reason', '')}"
        elif su == "doom_loop_detected":
            msg = "Doom loop detected"
        elif su == "auto_compact_started":
            msg = "Auto-compaction started"
        elif su == "auto_compact_completed":
            msg = "Auto-compaction completed"
        state.items.append(SystemItem(text=str(msg), kind=su))
        changes.append("system")
        return changes

    # ignore available_commands_update, git_branch_update, etc.
    return changes


def apply_events(state: ChatState, events: list[dict]) -> list[str]:
    all_c: list[str] = []
    for e in events:
        all_c.extend(apply_event(state, e))
    return all_c


def tool_snippet(item: ToolItem, n_lines: int = 4) -> str:
    """Display policy: default snippet for tools."""
    if not item.output:
        return ""
    lines = item.output.split("\n")
    return "\n".join(lines[:n_lines])
