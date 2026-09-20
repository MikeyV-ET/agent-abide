"""Claude native → ActivityEvent mapper + ClaudeBinaryStateObserver.

Native source: Claude Code session jsonl under ``~/.claude/projects/<cwd>/<sid>.jsonl``.

Shapes observed in a live Astro session (391 lines, 2026-09-20):

  {"type":"user","message":{"content":"<text>"}, "turnOrigin":"sdk", ...}
  {"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":...}]}}
  {"type":"assistant","message":{"model":"claude-opus-5","stop_reason":"tool_use",
                                 "content":[{"type":"thinking"|"text"|"tool_use",...}]}}
  chrome: attachment / queue-operation / atis-latch / last-prompt / cost-state / mode

Two things Claude does differently from grok, both handled here:

* **No turn_completed frame.** Turn boundaries live on the assistant line as
  ``stop_reason == "end_turn"`` (vs ``"tool_use"`` when more work follows), so a
  single line can be both the last speech of a turn and the turn's end. That is
  why the mapper returns a *list*.
* **Model identity is on every assistant line** (``message.model``). The observer
  turns a change in that value into one MODEL_INFO event, which is how the state
  file learns the model id.

Only ActivityKind is emitted — never a grok sessionUpdate name.
See docs/howto/binary_state_adapters.md
"""
from __future__ import annotations

import datetime
import time
from typing import Any, Callable, Optional

from binary_state.machine import BinaryActivityMachine
from binary_state.types import (
    DEFAULT_SILENCE_WINDOW,
    TIMEOUT_BUFFER,
    ActivityEvent,
    ActivityKind,
    ObserverState,
)

#: Session-file bookkeeping that says nothing about what the binary is doing.
#: These are skipped outright — mapping them to UNKNOWN would blank the state
#: every time one lands, and they outnumber real activity lines.
CHROME_TYPES = frozenset(
    {
        "attachment",
        "queue-operation",
        "atis-latch",
        "last-prompt",
        "cost-state",
        "mode",
        "summary",
        "file-history-snapshot",
    }
)

#: Claude tool names whose "silence" is a human not having answered yet.
#: Values must stay inside types.GATE_TOOL_KINDS so the machine says GATE, not STUCK.
GATE_TOOLS = {
    "ExitPlanMode": "exit_plan",
    "AskUserQuestion": "ask_user",
}


def _iso_to_epoch(value: str) -> Optional[float]:
    """'2026-09-20T04:30:53.515Z' → epoch seconds, or None if unparseable."""
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(text).timestamp()
    except (ValueError, AttributeError):
        return None


def _ts(obj: dict) -> float:
    raw = obj.get("timestamp")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        parsed = _iso_to_epoch(raw)
        if parsed is not None:
            return parsed
    return time.time()


def _tool_kind(name: str) -> str:
    return GATE_TOOLS.get(name, (name or "unknown").lower())


def _silence_for_tool_use(
    block: dict,
    silence_windows: dict[str, float],
    default: float,
) -> float:
    """How long this tool may sit quiet before the machine calls it stuck.

    Mirrors the grok mapper: an explicit timeout in the tool input wins, then a
    per-tool configured window, then the default.
    """
    params = block.get("input")
    if isinstance(params, dict):
        if isinstance(params.get("timeout_ms"), (int, float)):
            return (params["timeout_ms"] / 1000) * TIMEOUT_BUFFER
        # Claude's Bash tool spells it `timeout`, in milliseconds.
        if isinstance(params.get("timeout"), (int, float)):
            return (params["timeout"] / 1000) * TIMEOUT_BUFFER
    name = block.get("name") or ""
    if name in silence_windows:
        return silence_windows[name]
    return default


def map_claude_session_lines(
    obj: Any,
    *,
    known_types: set[str] | None = None,
    silence_windows: dict[str, float] | None = None,
    default_silence: float = DEFAULT_SILENCE_WINDOW,
) -> list[ActivityEvent]:
    """Map one Claude session jsonl object → zero or more ActivityEvents.

    Zero for chrome, more than one when a single line carries several content
    blocks or ends a turn.
    """
    if not isinstance(obj, dict):
        return []

    line_type = obj.get("type")
    if line_type in CHROME_TYPES:
        return []

    silence_windows = silence_windows or {}
    ts = _ts(obj)

    if line_type == "user":
        return _map_user(obj, ts)
    if line_type == "assistant":
        return _map_assistant(obj, ts, silence_windows, default_silence)

    # Something new in the format. Say so rather than guessing IDLE.
    return [
        ActivityEvent(
            kind=ActivityKind.UNKNOWN,
            source_type=f"claude:{line_type or 'unknown'}",
            known=False,
            ts=ts,
            native=obj,
        )
    ]


def map_claude_session_line(
    obj: Any,
    *,
    known_types: set[str] | None = None,
    silence_windows: dict[str, float] | None = None,
    default_silence: float = DEFAULT_SILENCE_WINDOW,
) -> Optional[ActivityEvent]:
    """First event for this line, or None. Prefer map_claude_session_lines.

    Kept because the handoff specified this signature; it drops trailing events
    (the TURN_END that rides along with a final SPEECH), so the observer uses the
    plural form.
    """
    events = map_claude_session_lines(
        obj,
        known_types=known_types,
        silence_windows=silence_windows,
        default_silence=default_silence,
    )
    return events[0] if events else None


def _map_user(obj: dict, ts: float) -> list[ActivityEvent]:
    content = (obj.get("message") or {}).get("content")
    events: list[ActivityEvent] = []

    if isinstance(content, str):
        if content.strip():
            events.append(
                ActivityEvent(
                    kind=ActivityKind.TURN_START,
                    source_type="claude:user",
                    ts=ts,
                    native=obj,
                )
            )
        return events

    if not isinstance(content, list):
        return events

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_result":
            events.append(
                ActivityEvent(
                    kind=ActivityKind.TOOL_END,
                    source_type="claude:tool_result",
                    ts=ts,
                    tool_id=block.get("tool_use_id"),
                    native=obj,
                )
            )
        elif btype == "text" and (block.get("text") or "").strip():
            events.append(
                ActivityEvent(
                    kind=ActivityKind.TURN_START,
                    source_type="claude:user",
                    ts=ts,
                    native=obj,
                )
            )
    return events


def _map_assistant(
    obj: dict,
    ts: float,
    silence_windows: dict[str, float],
    default_silence: float,
) -> list[ActivityEvent]:
    message = obj.get("message") or {}
    model_id = message.get("model")
    effort = obj.get("effort") or obj.get("perTurnEffort")
    content = message.get("content")
    events: list[ActivityEvent] = []

    blocks = content if isinstance(content, list) else []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "thinking":
            events.append(
                ActivityEvent(
                    kind=ActivityKind.THOUGHT,
                    source_type="claude:thinking",
                    ts=ts,
                    model_id=model_id,
                    reasoning_effort=effort,
                    native=obj,
                )
            )
        elif btype == "text":
            events.append(
                ActivityEvent(
                    kind=ActivityKind.SPEECH,
                    source_type="claude:text",
                    ts=ts,
                    model_id=model_id,
                    reasoning_effort=effort,
                    native=obj,
                )
            )
        elif btype == "tool_use":
            name = block.get("name") or ""
            events.append(
                ActivityEvent(
                    kind=ActivityKind.TOOL_START,
                    source_type=f"claude:tool_use:{name}" if name else "claude:tool_use",
                    ts=ts,
                    tool_id=block.get("id"),
                    tool_kind=_tool_kind(name),
                    tool_title=name,
                    raw_input=block.get("input"),
                    silence_s=_silence_for_tool_use(block, silence_windows, default_silence),
                    model_id=model_id,
                    reasoning_effort=effort,
                    native=obj,
                )
            )

    # Turn boundary rides on the same line as the final block.
    if message.get("stop_reason") == "end_turn":
        events.append(
            ActivityEvent(
                kind=ActivityKind.TURN_END,
                source_type="claude:end_turn",
                ts=ts,
                model_id=model_id,
                native=obj,
            )
        )
    return events


class ClaudeBinaryStateObserver:
    """Claude edge: session jsonl → ActivityEvent → BinaryActivityMachine.

    API mirrors GrokBinaryStateObserver.process_event(native_obj).
    """

    read_state_file = staticmethod(BinaryActivityMachine.read_state_file)

    def __init__(
        self,
        pid: int,
        known_types: set[str] | None = None,
        process_alive_fn: Callable[[int], bool] = None,
        silence_windows: dict[str, float] = None,
        event_silence_windows: dict[str, float] = None,
    ):
        self._known_types = known_types or set()
        self._silence_windows = silence_windows or {}
        self._seen_model_id: Optional[str] = None
        self._machine = BinaryActivityMachine(
            pid=pid,
            process_alive_fn=process_alive_fn,
            silence_windows=silence_windows or {},
            event_silence_windows=event_silence_windows or {},
        )

    @property
    def state(self) -> ObserverState:
        return self._machine.state

    @property
    def doom_loop(self) -> bool:
        return self._machine.doom_loop

    def process_event(self, obj: dict):
        """Claude session line (dict) → machine."""
        for ev in map_claude_session_lines(
            obj,
            known_types=self._known_types,
            silence_windows=self._silence_windows,
        ):
            self._note_model(ev)
            self._machine.apply(ev)

    def _note_model(self, ev: ActivityEvent) -> None:
        """Announce the model the first time it appears, and on any change.

        Claude stamps every assistant line with its model; the machine only reads
        model_id off MODEL_INFO, so synthesize one rather than emitting a
        metadata event per line (which would inflate turn_event_count).
        """
        if not ev.model_id or ev.model_id == self._seen_model_id:
            return
        self._seen_model_id = ev.model_id
        self._machine.apply(
            ActivityEvent(
                kind=ActivityKind.MODEL_INFO,
                source_type="claude:model",
                ts=ev.ts,
                model_id=ev.model_id,
                reasoning_effort=ev.reasoning_effort,
            )
        )

    def check_heartbeat(self):
        self._machine.check_heartbeat()

    def reset(self, new_pid: int, session_dir: str = None):
        self._seen_model_id = None
        self._machine.reset(new_pid, session_dir)

    def state_dict(self) -> dict:
        return self._machine.state_dict()

    def write_state_file(self, path: str):
        self._machine.write_state_file(path)

    def orient_from_history(self, events: list[dict]):
        for e in events or []:
            self.process_event(e)
