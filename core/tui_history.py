"""TUI history SoR: prefer asdaaas/history/hot.jsonl (aa.stream).

P0: path resolve + parse.
P1: live tail helpers — map aa.stream events → grok-shaped updates so
existing TUI _dispatch_event / ChatState reducers keep working.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator, Optional

from aa_stream_parser import is_aa_stream_path, parse_aa_stream


def agent_history_dir(agent_home: Path) -> Path:
    return Path(agent_home) / "asdaaas" / "history"


def hot_jsonl_path(agent_home: Path) -> Path:
    return agent_history_dir(agent_home) / "hot.jsonl"


def updates_jsonl_candidates(agent_home: Path) -> list[Path]:
    """Legacy grok session updates paths (best-effort)."""
    home = Path(agent_home)
    cands = []
    for p in [
        home / "asdaaas" / "updates.jsonl",
        home / "updates.jsonl",
    ]:
        if p.exists():
            cands.append(p)
    return cands


def resolve_history_source(agent_home: Path, prefer: Optional[str] = None) -> tuple[str, Path]:
    """Return (kind, path) kind in hot|updates|none.

    prefer: env TUI_HISTORY_SOURCE or arg: hot|updates|auto
    """
    prefer = (prefer or os.environ.get("TUI_HISTORY_SOURCE") or "auto").lower()
    hot = hot_jsonl_path(agent_home)
    if prefer == "hot":
        return ("hot", hot) if hot.exists() else ("none", hot)
    if prefer == "updates":
        ups = updates_jsonl_candidates(agent_home)
        return ("updates", ups[0]) if ups else ("none", hot)
    # auto
    if hot.exists() and hot.stat().st_size > 0:
        return ("hot", hot)
    ups = updates_jsonl_candidates(agent_home)
    if ups:
        return ("updates", ups[0])
    return ("none", hot)


def entries_from_hot(path: Path) -> list[dict[str, Any]]:
    return list(parse_aa_stream(path))


def entry_to_tui_lines(entry: dict[str, Any]) -> list[str]:
    """Minimal paint lines for catch-up (P0)."""
    role = (entry.get("role") or entry.get("type") or "").lower()
    text = entry.get("content") or entry.get("text") or ""
    if isinstance(text, list):
        text = " ".join(
            (x.get("text") if isinstance(x, dict) else str(x)) for x in text
        )
    text = str(text).strip()
    if not text:
        return []
    if role in ("user", "human"):
        return [f"You: {text}"]
    if role in ("assistant", "agent"):
        return [text]
    if role in ("system", "control"):
        return [f"[{text}]"]
    return [text]


def aa_event_to_tui_update(ev: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Map one aa.stream v1 event → grok session/update shape for TUI dispatch.

    Returns None when the event has nothing to paint (meta chrome, empty).
    """
    if not isinstance(ev, dict):
        return None
    if ev.get("format") and ev.get("format") != "aa.stream":
        return None

    body = ev.get("body") or {}
    kind = body.get("kind") or ""
    role = (ev.get("role") or "").lower()
    text = body.get("text") if isinstance(body.get("text"), str) else ""
    ts = ev.get("ts")

    def _frame(session_update: str, update: dict) -> dict:
        u = dict(update)
        u["sessionUpdate"] = session_update
        return {
            "timestamp": ts,
            "method": "session/update",
            "params": {"update": u},
            # breadcrumb for debugging / dual-path
            "_aa_stream": True,
            "_aa_class": ev.get("class"),
            "_aa_seq": ev.get("stream_seq"),
        }

    if kind in ("text_delta", "text"):
        if not text:
            return None
        content = {"text": text} if kind == "text_delta" or True else text
        # TUI chunk handlers expect content as {"text": ...}
        if role in ("user", "human"):
            return _frame("user_message_chunk", {"content": {"text": text}})
        # assistant / agent / default
        return _frame("agent_message_chunk", {"content": {"text": text}})

    if kind in ("thinking_delta", "thinking"):
        if not text:
            return None
        return _frame("agent_thought_chunk", {"content": {"text": text}})

    if kind == "tool_call":
        tid = body.get("id") or body.get("tool_id") or ""
        name = body.get("name") or "tool"
        update = {
            "toolCallId": str(tid) if tid is not None else "",
            "title": name,
            "name": name,
            "rawInput": body.get("args") or body.get("input"),
            "status": body.get("status") or "started",
        }
        return _frame("tool_call", update)

    if kind == "tool_result":
        tid = body.get("tool_id") or body.get("id") or ""
        content = body.get("content") or text or ""
        update = {
            "toolCallId": str(tid) if tid is not None else "",
            "status": body.get("status") or "completed",
            "rawOutput": content,
            "content": content,
        }
        return _frame("tool_call_update", update)

    # meta / usage / raw_only — no paint
    return None


def iter_hot_tui_events_from_offset(
    path: Path, offset: int
) -> tuple[list[dict[str, Any]], int]:
    """Read complete hot.jsonl lines from offset → TUI update events + new offset."""
    from aa_stream_parser import iter_aa_stream_lines_from_offset

    raw, new_off = iter_aa_stream_lines_from_offset(str(path), offset)
    out = []
    for ev in raw:
        u = aa_event_to_tui_update(ev)
        if u is not None:
            out.append(u)
    return out, new_off


SPEECH_SESSION_UPDATES = frozenset({
    "user_message_chunk",
    "agent_message_chunk",
    "agent_thought_chunk",
})


def is_speech_tui_event(event: dict[str, Any]) -> bool:
    """True if a TUI update event carries visible speech/thought text."""
    if not isinstance(event, dict):
        return False
    update = (event.get("params") or {}).get("update") or {}
    su = update.get("sessionUpdate") or ""
    if su not in SPEECH_SESSION_UPDATES:
        return False
    c = update.get("content") or {}
    t = c.get("text", "") if isinstance(c, dict) else (c if isinstance(c, str) else "")
    return bool(str(t).strip())


def line_to_tui_event(line: str, hist_kind: str = "updates") -> Optional[dict[str, Any]]:
    """Parse one jsonl line from hot or updates into a TUI dispatch event."""
    import json as _json
    try:
        obj = _json.loads(line)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if hist_kind == "hot" or (
        obj.get("format") == "aa.stream" or (obj.get("body") and obj.get("class"))
    ):
        return aa_event_to_tui_update(obj)
    # already grok-shaped
    if (obj.get("params") or {}).get("update") is not None:
        return obj
    return None


def select_tail_events(
    events: list[dict[str, Any]],
    n: int,
    *,
    speech_first: bool = True,
) -> list[dict[str, Any]]:
    """Take a tail window of N items.

    When speech_first (default), N counts *speech* events (user/agent/thought
    with text). Tools inside that span are kept so context is not stripped.
    """
    if n is None or n <= 0 or len(events) <= n and not speech_first:
        return list(events)
    if not speech_first:
        return events[-n:] if len(events) > n else list(events)

    speech = 0
    start = len(events)
    for i in range(len(events) - 1, -1, -1):
        start = i
        if is_speech_tui_event(events[i]):
            speech += 1
            if speech >= n:
                break
    return events[start:]
