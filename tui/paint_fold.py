"""hot.jsonl events → display model (PaintUnits via ChatState).

Shared fold for live tail and history catch-up. Product law (Eric 2026-09-22):

- User non-system speech, agent speech, thinking → FULL (sanity caps only)
- Tools → SHOW but TRUNCATED (snippet; expand is UI)
- System / retry / aa.control → SHOW (banner); verbosity TBD
- Meta noise (turn_completed, etc.) → DROP or thin system note

File atom = one aa.stream jsonl line.
Paint unit = SpeechItem | ThinkingItem | ToolItem | SystemItem | TurnMark
  (ChatState items). Many file events may fold into one paint unit (tool id,
  streaming speech).

History loop should: read batch of lines → fold → ask "enough paint?" → repeat.
Not: budget raw bytes as if they were screen lines.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional

from chat_model import (
    ChatState,
    SpeechItem,
    ThinkingItem,
    ToolItem,
    PlanItem,
    SystemItem,
    TurnMark,
    apply_event,
    prune_items,
    DEFAULT_MAX_ITEMS,
)


class DisplayPolicy(str, Enum):
    FULL = "full"          # entire text on glass (user/agent/thinking)
    SNIPPET = "snippet"    # tools: title + short body
    BANNER = "banner"      # retry, control, short system
    DROP = "drop"          # not painted


def policy_for_item(item: Any) -> DisplayPolicy:
    if isinstance(item, (SpeechItem, ThinkingItem)):
        return DisplayPolicy.FULL
    if isinstance(item, ToolItem):
        return DisplayPolicy.SNIPPET
    if isinstance(item, PlanItem):
        return DisplayPolicy.FULL  # full plan table in purple panel
    if isinstance(item, SystemItem):
        return DisplayPolicy.BANNER
    if isinstance(item, TurnMark):
        return DisplayPolicy.BANNER
    return DisplayPolicy.DROP


DROP_SESSION_UPDATES = frozenset({
    "turn_completed",
    "turn_started",
    "available_commands_update",
    "current_mode_update",
    "session_info",
    "background_tasks",
    "auto_compact_started",
    "auto_compact_completed",
    "compaction_checkpoint",
    "memory_dream_queued",
    "memory_dream_started",
    "memory_dream_completed",
    "hook_execution",
})


@dataclass
class FoldResult:
    state: ChatState
    changes: list[str] = field(default_factory=list)
    events_in: int = 0
    events_applied: int = 0
    events_dropped: int = 0


def _ensure_core_path() -> None:
    core = str(Path(__file__).resolve().parent.parent / "core")
    if core not in sys.path:
        sys.path.insert(0, core)


def event_from_hot_line(line: str) -> Optional[dict]:
    _ensure_core_path()
    from tui_history import line_to_tui_event
    return line_to_tui_event(line, "hot")


def should_apply_event(event: dict) -> bool:
    su = ((event.get("params") or {}).get("update") or {}).get("sessionUpdate") or ""
    if not su:
        return False
    if su in DROP_SESSION_UPDATES:
        return False
    return True


def fold_event(state: ChatState, event: dict) -> list[str]:
    if not should_apply_event(event):
        return []
    su = ((event.get("params") or {}).get("update") or {}).get("sessionUpdate")
    if su == "retry_state":
        u = dict((event.get("params") or {}).get("update") or {})
        attempt = u.get("attempt", "?")
        mx = u.get("max_retries", "?")
        reason = u.get("reason") or ""
        u["message"] = f"Retry {attempt}/{mx}: {reason}"
        event = {
            **event,
            "params": {**(event.get("params") or {}), "update": u},
        }
    return apply_event(state, event)


def fold_events(state: ChatState, events: Iterable[dict]) -> FoldResult:
    changes: list[str] = []
    n_in = 0
    n_app = 0
    n_drop = 0
    for ev in events:
        n_in += 1
        if not isinstance(ev, dict):
            n_drop += 1
            continue
        if not should_apply_event(ev):
            n_drop += 1
            continue
        ch = fold_event(state, ev)
        n_app += 1
        changes.extend(ch)
    return FoldResult(
        state=state,
        changes=changes,
        events_in=n_in,
        events_applied=n_app,
        events_dropped=n_drop,
    )


def fold_hot_lines(state: ChatState, lines: Iterable[str]) -> FoldResult:
    events: list[dict] = []
    dropped = 0
    n_in = 0
    for line in lines:
        n_in += 1
        if not line or not str(line).strip():
            dropped += 1
            continue
        ev = event_from_hot_line(str(line))
        if ev is None:
            dropped += 1
            continue
        events.append(ev)
    result = fold_events(state, events)
    result.events_in = n_in
    result.events_dropped += dropped
    return result


def paint_units(state: ChatState) -> list[tuple[Any, DisplayPolicy]]:
    out = []
    for item in state.items:
        pol = policy_for_item(item)
        if pol is DisplayPolicy.DROP:
            continue
        out.append((item, pol))
    return out


def count_meaningful_paint(state: ChatState) -> dict[str, int]:
    n_speech = n_think = n_tool = n_sys = n_turn = n_plan = 0
    for item, _pol in paint_units(state):
        if isinstance(item, SpeechItem):
            n_speech += 1
        elif isinstance(item, ThinkingItem):
            n_think += 1
        elif isinstance(item, ToolItem):
            n_tool += 1
        elif isinstance(item, PlanItem):
            n_plan += 1
        elif isinstance(item, SystemItem):
            n_sys += 1
        elif isinstance(item, TurnMark):
            n_turn += 1
    return {
        "speech": n_speech,
        "thinking": n_think,
        "tools": n_tool,
        "plans": n_plan,
        "system": n_sys,
        "turns": n_turn,
        "total": n_speech + n_think + n_tool + n_plan + n_sys + n_turn,
        "full": n_speech + n_think + n_plan,
        "snippet": n_tool,
    }


def enough_for_tip(
    state: ChatState,
    *,
    min_full: int = 12,
    min_total: int = 20,
) -> bool:
    c = count_meaningful_paint(state)
    return c["full"] >= min_full and c["total"] >= min_total


def fold_hot_file_tail(
    path: str | Path,
    *,
    max_bytes: int = 8 * 1024 * 1024,
    state: Optional[ChatState] = None,
    prune_max: int = DEFAULT_MAX_ITEMS,
) -> FoldResult:
    path = Path(path)
    state = state or ChatState()
    if not path.exists():
        return FoldResult(state=state)
    size = path.stat().st_size
    seek = max(0, size - max_bytes)
    with path.open("rb") as f:
        f.seek(seek)
        if seek > 0:
            f.readline()
        raw = f.read().decode("utf-8", errors="replace")
    result = fold_hot_lines(state, raw.split("\n"))
    prune_items(state, prune_max)
    result.state = state
    return result


def fold_tui_events_oldest_first(events: list[dict]) -> FoldResult:
    """Fold a chronological list of TUI dispatch events (history batch)."""
    state = ChatState()
    return fold_events(state, events)
