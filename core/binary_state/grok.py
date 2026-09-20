"""Grok native → ActivityEvent mapper + GrokBinaryStateObserver.

Native sources:
  - updates.jsonl frames: {params: {update: {sessionUpdate, ...}}}
  - stdout JSON-RPC: _x.ai/session_notification, sessions/changed, models/update
"""
from __future__ import annotations

import json
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


def _silence_from_tool_update(
    update: dict,
    silence_windows: dict[str, float],
    default: float,
) -> float:
    tool_name = update.get("title", "") or ""
    raw_input = update.get("rawInput", "")
    if raw_input:
        try:
            params = json.loads(raw_input) if isinstance(raw_input, str) else raw_input
            if isinstance(params, dict):
                if "timeout_ms" in params:
                    return (params["timeout_ms"] / 1000) * TIMEOUT_BUFFER
                if "timeout" in params and isinstance(params["timeout"], (int, float)):
                    return float(params["timeout"]) * TIMEOUT_BUFFER
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    if tool_name in silence_windows:
        return silence_windows[tool_name]
    return default


def map_grok_updates_frame(
    frame: dict,
    *,
    known_types: set[str],
    silence_windows: dict[str, float] | None = None,
    default_silence: float = DEFAULT_SILENCE_WINDOW,
) -> Optional[ActivityEvent]:
    """Map one grok updates.jsonl object → ActivityEvent (or None to skip)."""
    if not isinstance(frame, dict):
        return None
    try:
        update = frame["params"]["update"]
        event_type = update.get("sessionUpdate") or "unknown"
    except (KeyError, TypeError):
        return ActivityEvent(
            kind=ActivityKind.UNKNOWN,
            source_type="unknown",
            known=False,
            ts=_ts(frame),
            native=frame,
        )

    silence_windows = silence_windows or {}
    ts = _ts(frame)

    if event_type not in known_types:
        return ActivityEvent(
            kind=ActivityKind.UNKNOWN,
            source_type=event_type,
            known=False,
            ts=ts,
            native=frame,
        )

    if event_type == "user_message_chunk":
        return ActivityEvent(
            kind=ActivityKind.TURN_START, source_type=event_type, ts=ts, native=frame
        )

    if event_type == "turn_completed":
        return ActivityEvent(
            kind=ActivityKind.TURN_END, source_type=event_type, ts=ts, native=frame
        )

    if event_type == "retry_state":
        retry_type = update.get("type", "")
        if retry_type == "retrying":
            return ActivityEvent(
                kind=ActivityKind.RETRYING,
                source_type=event_type,
                ts=ts,
                retry_attempt=update.get("attempt"),
                retry_reason=update.get("reason"),
                native=frame,
            )
        if retry_type == "failed":
            return ActivityEvent(
                kind=ActivityKind.RETRY_FAILED,
                source_type=event_type,
                ts=ts,
                native=frame,
            )
        return ActivityEvent(
            kind=ActivityKind.KNOWN_OTHER, source_type=event_type, ts=ts, native=frame
        )

    if event_type == "tool_call":
        tool_id = update.get("toolCallId")
        meta = update.get("_meta", {}) or {}
        tool_info = meta.get("x.ai/tool", {}) or {}
        kind = tool_info.get("kind", "unknown")
        return ActivityEvent(
            kind=ActivityKind.TOOL_START,
            source_type=event_type,
            ts=ts,
            tool_id=tool_id,
            tool_kind=kind,
            tool_title=update.get("title") or "",
            raw_input=update.get("rawInput"),
            silence_s=_silence_from_tool_update(
                update, silence_windows, default_silence
            ),
            native=frame,
        )

    if event_type == "tool_call_update":
        tool_id = update.get("toolCallId")
        status = update.get("status", "")
        if tool_id and status in ("completed", "failed"):
            return ActivityEvent(
                kind=ActivityKind.TOOL_END,
                source_type=event_type,
                ts=ts,
                tool_id=tool_id,
                native=frame,
            )
        # progress — still activity
        return ActivityEvent(
            kind=ActivityKind.KNOWN_OTHER, source_type=event_type, ts=ts, native=frame
        )

    if event_type == "doom_loop_detected":
        return ActivityEvent(
            kind=ActivityKind.DOOM, source_type=event_type, ts=ts, native=frame
        )

    if event_type == "agent_message_chunk":
        return ActivityEvent(
            kind=ActivityKind.SPEECH, source_type=event_type, ts=ts, native=frame
        )

    if event_type == "agent_thought_chunk":
        return ActivityEvent(
            kind=ActivityKind.THOUGHT, source_type=event_type, ts=ts, native=frame
        )

    return ActivityEvent(
        kind=ActivityKind.KNOWN_OTHER, source_type=event_type, ts=ts, native=frame
    )


def map_grok_stdout_frame(frame: dict) -> list[ActivityEvent]:
    """Map grok binary stdout JSON-RPC → zero or more ActivityEvents."""
    if not isinstance(frame, dict):
        return []
    method = frame.get("method", "") or ""
    out: list[ActivityEvent] = []
    ts = _ts(frame)

    if method == "_x.ai/session_notification":
        params = frame.get("params") or {}
        update = params.get("update") or {}
        su = update.get("sessionUpdate", "")
        if su == "model_changed":
            out.append(
                ActivityEvent(
                    kind=ActivityKind.MODEL_INFO,
                    source_type=su,
                    ts=ts,
                    model_id=update.get("model_id"),
                    reasoning_effort=update.get("reasoning_effort"),
                    native=frame,
                )
            )
        elif su == "session_changed":
            out.append(
                ActivityEvent(
                    kind=ActivityKind.SESSION_ACTIVITY,
                    source_type=su,
                    ts=ts,
                    activity=update.get("activity"),
                    model_id=update.get("model_id"),
                    reasoning_effort=update.get("reasoning_effort"),
                    native=frame,
                )
            )

    elif method == "_x.ai/sessions/changed":
        params = frame.get("params") or {}
        out.append(
            ActivityEvent(
                kind=ActivityKind.SESSION_ACTIVITY,
                source_type="sessions/changed",
                ts=ts,
                model_id=params.get("model_id"),
                reasoning_effort=params.get("reasoning_effort"),
                activity=params.get("activity"),
                native=frame,
            )
        )

    elif method == "_x.ai/models/update":
        # available_models handled specially on observer (list payload)
        out.append(
            ActivityEvent(
                kind=ActivityKind.MODEL_INFO,
                source_type="models/update",
                ts=ts,
                native=frame,
            )
        )

    return out


def _ts(frame: dict) -> float:
    t = frame.get("timestamp")
    if isinstance(t, (int, float)):
        return float(t)
    return time.time()


class GrokBinaryStateObserver:
    """Grok edge: native frames → ActivityEvent → BinaryActivityMachine.

    Public API matches legacy BinaryStateObserver so asdaaas/tests keep working:
      process_event(updates.jsonl frame)
      process_stdout_event(stdout frame)
      check_heartbeat, state_dict, write_state_file, …
    """

    # expose nested machine helpers used as staticmethods in tests
    read_proc_state = staticmethod(BinaryActivityMachine.read_proc_state)
    read_exit_code = staticmethod(BinaryActivityMachine.read_exit_code)
    read_state_file = staticmethod(BinaryActivityMachine.read_state_file)

    def __init__(
        self,
        pid: int,
        known_types: set[str],
        process_alive_fn: Callable[[int], bool] = None,
        silence_windows: dict[str, float] = None,
        event_silence_windows: dict[str, float] = None,
    ):
        self._known_types = known_types
        self._silence_windows = silence_windows or {}
        self._event_silence_windows = event_silence_windows or {}
        self._machine = BinaryActivityMachine(
            pid=pid,
            process_alive_fn=process_alive_fn,
            silence_windows=self._silence_windows,
            event_silence_windows=self._event_silence_windows,
        )

    # -- delegate surface --
    @property
    def pid(self):
        return self._machine.pid

    @pid.setter
    def pid(self, v):
        self._machine.pid = v

    @property
    def state(self) -> ObserverState:
        return self._machine.state

    @property
    def since(self):
        return self._machine.since

    @property
    def last_event_type(self):
        return self._machine.last_event_type

    @property
    def last_event_ts(self):
        return self._machine.last_event_ts

    @property
    def retry_attempt(self):
        return self._machine.retry_attempt

    @property
    def retry_reason(self):
        return self._machine.retry_reason

    @property
    def doom_loop(self):
        return self._machine.doom_loop

    @property
    def unknown_event(self):
        return self._machine.unknown_event

    @property
    def has_pending_tool_calls(self):
        return self._machine.has_pending_tool_calls

    @property
    def turn_event_count(self):
        return self._machine.turn_event_count

    @property
    def model_id(self):
        return self._machine.model_id

    @property
    def reasoning_effort(self):
        return self._machine.reasoning_effort

    @property
    def activity(self):
        return self._machine.activity

    @property
    def available_models(self):
        return self._machine.available_models

    # tests poke private fields on observer — proxy to machine
    @property
    def _state(self):
        return self._machine._state

    @_state.setter
    def _state(self, v):
        self._machine._state = v

    @property
    def _last_event_type(self):
        return self._machine._last_event_type

    @_last_event_type.setter
    def _last_event_type(self, v):
        self._machine._last_event_type = v

    @property
    def _last_event_ts(self):
        return self._machine._last_event_ts

    @_last_event_ts.setter
    def _last_event_ts(self, v):
        self._machine._last_event_ts = v

    @property
    def _pending_tools(self):
        return self._machine._pending_tools

    @property
    def _turn_event_count(self):
        return self._machine._turn_event_count

    @_turn_event_count.setter
    def _turn_event_count(self, v):
        self._machine._turn_event_count = v

    @property
    def _model_id(self):
        return self._machine._model_id

    @_model_id.setter
    def _model_id(self, v):
        self._machine._model_id = v

    @property
    def _reasoning_effort(self):
        return self._machine._reasoning_effort

    @_reasoning_effort.setter
    def _reasoning_effort(self, v):
        self._machine._reasoning_effort = v


    @property
    def _process_alive_fn(self):
        return self._machine._process_alive_fn

    @_process_alive_fn.setter
    def _process_alive_fn(self, fn):
        self._machine._process_alive_fn = fn

    @property
    def _expected_silence(self):
        return self._machine._expected_silence

    @_expected_silence.setter
    def _expected_silence(self, v):
        self._machine._expected_silence = v

    def process_event(self, frame: dict):
        """Grok updates.jsonl frame → machine."""
        ev = map_grok_updates_frame(
            frame,
            known_types=self._known_types,
            silence_windows=self._silence_windows,
        )
        if ev is not None:
            self._machine.apply(ev)

    def process_stdout_event(self, frame: dict):
        method = (frame or {}).get("method", "")
        if method == "_x.ai/models/update":
            models = (frame.get("params") or {}).get("models")
            if models is not None:
                self._machine.set_available_models(models)
        for ev in map_grok_stdout_frame(frame):
            self._machine.apply(ev)

    def check_heartbeat(self):
        self._machine.check_heartbeat()

    def reset(self, new_pid: int, session_dir: str = None):
        self._machine.reset(new_pid, session_dir)

    def orient_from_history(self, events: list[dict]):
        if not events:
            return
        for event in events:
            self.process_event(event)

    def state_dict(self) -> dict:
        return self._machine.state_dict()

    def write_state_file(self, path: str):
        self._machine.write_state_file(path)
