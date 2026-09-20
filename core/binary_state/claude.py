"""Claude native → ActivityEvent mapper + ClaudeBinaryStateObserver.

*** FOR OPUS / Claude-backend work ***

Implement ``map_claude_session_line`` (and optional stdout/SDK feed) so
Claude Code session jsonl becomes ActivityEvent. Do **not** emit grok
sessionUpdate names — only ActivityKind.

Native (Claude Code session jsonl), typical lines:
  {"type":"user","message":{"role":"user","content":...}, ...}
  {"type":"assistant","message":{"content":[{"type":"text"|"tool_use"|"thinking",...}]}}
  tool_result often arrives as user message content blocks

Suggested mapping (edit freely, keep ActivityKind stable):
  type=user with real user text     → TURN_START (or SPEECH if mid-turn tool_result-only)
  type=user tool_result-only        → TOOL_END (tool_id from tool_use_id)
  type=assistant text               → SPEECH
  type=assistant thinking           → THOUGHT
  type=assistant tool_use           → TOOL_START (id, name→tool_kind/title)
  type=result / end markers         → TURN_END if Claude exposes turn boundaries
  unknown                           → UNKNOWN or KNOWN_OTHER

Wire-up later:
  ClaudeBackend feeds session lines → ClaudeBinaryStateObserver.process_event
  Same BinaryActivityMachine / state file / health plane as grok.

See docs/howto/binary_state_adapters.md
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from binary_state.machine import BinaryActivityMachine
from binary_state.types import ActivityEvent, ActivityKind, ObserverState


def map_claude_session_line(
    obj: dict,
    *,
    known_types: set[str] | None = None,
) -> Optional[ActivityEvent]:
    """Map one Claude session jsonl object → ActivityEvent.

    TODO(opus): implement. Return None to skip chrome (attachment, etc.).
    """
    if not isinstance(obj, dict):
        return None
    # Placeholder: mark unknown so machine does not pretend IDLE
    t = obj.get("type") or "unknown"
    return ActivityEvent(
        kind=ActivityKind.UNKNOWN,
        source_type=f"claude:{t}",
        known=False,
        ts=_ts(obj),
        native=obj,
    )


def _ts(obj: dict) -> float:
    t = obj.get("timestamp")
    if isinstance(t, (int, float)):
        return float(t)
    # ISO strings — best-effort leave to implementer
    return time.time()


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
        ev = map_claude_session_line(obj, known_types=self._known_types)
        if ev is not None:
            self._machine.apply(ev)

    def check_heartbeat(self):
        self._machine.check_heartbeat()

    def reset(self, new_pid: int, session_dir: str = None):
        self._machine.reset(new_pid, session_dir)

    def state_dict(self) -> dict:
        return self._machine.state_dict()

    def write_state_file(self, path: str):
        self._machine.write_state_file(path)

    def orient_from_history(self, events: list[dict]):
        for e in events or []:
            self.process_event(e)
