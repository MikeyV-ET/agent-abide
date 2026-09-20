"""BinaryActivityMachine — backend-agnostic BUSY/IDLE/STUCK/… state machine."""
from __future__ import annotations

import json
import os
import time
from typing import Callable, Optional

from binary_state.types import (
    DEFAULT_SILENCE_WINDOW,
    GATE_TOOL_KINDS,
    STATE_TTL,
    TIMEOUT_BUFFER,
    ActivityEvent,
    ActivityKind,
    ObserverState,
)


class BinaryActivityMachine:
    """Core state machine. Feed ActivityEvent only — no native formats here."""

    def __init__(
        self,
        pid: int,
        process_alive_fn: Callable[[int], bool] = None,
        silence_windows: dict[str, float] = None,
        event_silence_windows: dict[str, float] = None,
        default_silence: float = None,
    ):
        self.pid = pid
        self._process_alive_fn = process_alive_fn or self._default_process_check
        self._silence_windows = silence_windows or {}
        self._event_silence_windows = event_silence_windows or {}
        self._default_silence = (
            default_silence if default_silence is not None else DEFAULT_SILENCE_WINDOW
        )

        self._state = ObserverState.STARTING
        self._since = time.time()
        self._last_event_type: Optional[str] = None
        self._last_event_ts: Optional[float] = None
        self._pending_tools: dict[str, str] = {}
        self._expected_silence: float = self._default_silence
        self._turn_event_count: int = 0

        self._retry_attempt: Optional[int] = None
        self._retry_reason: Optional[str] = None
        self._doom_loop: bool = False
        self._unknown_event: Optional[str] = None
        self._exit_code: Optional[int] = None
        self._pid_proc_state: Optional[str] = None

        self._model_id: str = "unknown"
        self._reasoning_effort: Optional[str] = None
        self._activity: Optional[str] = None
        self._available_models: Optional[list] = None

    # -- properties (same surface as legacy BinaryStateObserver) --

    @property
    def state(self) -> ObserverState:
        return self._state

    @property
    def since(self) -> float:
        return self._since

    @property
    def last_event_type(self) -> Optional[str]:
        return self._last_event_type

    @property
    def last_event_ts(self) -> Optional[float]:
        return self._last_event_ts

    @property
    def retry_attempt(self) -> Optional[int]:
        return self._retry_attempt

    @property
    def retry_reason(self) -> Optional[str]:
        return self._retry_reason

    @property
    def doom_loop(self) -> bool:
        return self._doom_loop

    @property
    def unknown_event(self) -> Optional[str]:
        return self._unknown_event

    @property
    def has_pending_tool_calls(self) -> bool:
        return bool(self._pending_tools)

    @property
    def turn_event_count(self) -> int:
        return self._turn_event_count

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def reasoning_effort(self) -> Optional[str]:
        return self._reasoning_effort

    @property
    def activity(self) -> Optional[str]:
        return self._activity

    @property
    def available_models(self) -> Optional[list]:
        return self._available_models

    def set_available_models(self, models: list) -> None:
        self._available_models = models

    # -- process helpers --

    @staticmethod
    def _default_process_check(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def read_proc_state(pid: int) -> Optional[str]:
        try:
            with open(f"/proc/{pid}/stat") as f:
                line = f.read()
                close_paren = line.rfind(")")
                if close_paren >= 0 and close_paren + 2 < len(line):
                    return line[close_paren + 2]
        except (FileNotFoundError, PermissionError, OSError):
            pass
        return None

    @staticmethod
    def read_exit_code(pid: int) -> Optional[int]:
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                if os.WIFEXITED(status):
                    return os.WEXITSTATUS(status)
                if os.WIFSIGNALED(status):
                    return 128 + os.WTERMSIG(status)
        except ChildProcessError:
            pass
        return None

    def _set_state(self, new_state: ObserverState):
        if new_state != self._state:
            self._state = new_state
            self._since = time.time()

    # -- core --

    def apply(self, ev: ActivityEvent) -> None:
        """Apply one normalized activity event."""
        if ev is None:
            return
        now = ev.ts or time.time()
        label = ev.source_type or ev.kind.value
        self._last_event_type = label
        self._last_event_ts = now

        if ev.kind == ActivityKind.UNKNOWN or not ev.known:
            self._set_state(ObserverState.UNKNOWN)
            self._unknown_event = label
            return

        if self._state == ObserverState.UNKNOWN:
            self._unknown_event = None

        if ev.kind == ActivityKind.TURN_START:
            self._set_state(ObserverState.BUSY)
            self._pending_tools.clear()
            self._turn_event_count = 1
            self._doom_loop = False
            self._retry_attempt = None
            self._retry_reason = None
            self._expected_silence = self._event_silence_windows.get(
                label, self._default_silence
            )
            return

        self._turn_event_count += 1

        if ev.kind == ActivityKind.TURN_END:
            self._set_state(ObserverState.IDLE)
            self._pending_tools.clear()
            self._turn_event_count = 0

        elif ev.kind == ActivityKind.RETRYING:
            self._set_state(ObserverState.RETRYING)
            self._retry_attempt = ev.retry_attempt
            self._retry_reason = ev.retry_reason

        elif ev.kind == ActivityKind.RETRY_FAILED:
            self._set_state(ObserverState.BUSY)

        elif ev.kind == ActivityKind.TOOL_START:
            if ev.tool_id:
                self._pending_tools[ev.tool_id] = ev.tool_kind or "unknown"
            if ev.silence_s is not None:
                self._expected_silence = ev.silence_s
            else:
                self._expected_silence = self._silence_for_tool(ev)
            if self._state not in (ObserverState.BUSY,):
                self._set_state(ObserverState.BUSY)

        elif ev.kind == ActivityKind.TOOL_END:
            if ev.tool_id:
                self._pending_tools.pop(ev.tool_id, None)
            if self._state not in (ObserverState.BUSY,):
                self._set_state(ObserverState.BUSY)

        elif ev.kind == ActivityKind.DOOM:
            self._doom_loop = True

        elif ev.kind in (ActivityKind.SPEECH, ActivityKind.THOUGHT, ActivityKind.KNOWN_OTHER):
            if self._state in (
                ObserverState.RETRYING,
                ObserverState.UNKNOWN,
                ObserverState.STARTING,
            ):
                self._set_state(ObserverState.BUSY)
            elif ev.kind == ActivityKind.KNOWN_OTHER and self._state not in (
                ObserverState.BUSY,
                ObserverState.IDLE,
                ObserverState.GATE,
                ObserverState.STUCK,
                ObserverState.GONE,
            ):
                self._set_state(ObserverState.BUSY)

        elif ev.kind == ActivityKind.MODEL_INFO:
            if ev.model_id:
                self._model_id = ev.model_id
            if ev.reasoning_effort:
                self._reasoning_effort = ev.reasoning_effort
            return  # no silence bump

        elif ev.kind == ActivityKind.SESSION_ACTIVITY:
            if ev.activity is not None:
                self._activity = ev.activity
            if ev.model_id:
                self._model_id = ev.model_id
            if ev.reasoning_effort:
                self._reasoning_effort = ev.reasoning_effort
            return

        # silence from event type (tool_start sets its own)
        if ev.kind != ActivityKind.TOOL_START:
            self._expected_silence = self._event_silence_windows.get(
                label, self._default_silence
            )

    def _silence_for_tool(self, ev: ActivityEvent) -> float:
        raw = ev.raw_input
        if raw:
            try:
                params = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(params, dict):
                    if "timeout_ms" in params:
                        return (params["timeout_ms"] / 1000) * TIMEOUT_BUFFER
                    if "timeout" in params and isinstance(params["timeout"], (int, float)):
                        return float(params["timeout"]) * TIMEOUT_BUFFER
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        if ev.tool_title and ev.tool_title in self._silence_windows:
            return self._silence_windows[ev.tool_title]
        return self._default_silence

    def check_heartbeat(self):
        self._pid_proc_state = self.read_proc_state(self.pid)
        if not self._process_alive_fn(self.pid):
            self._exit_code = self.read_exit_code(self.pid)
            self._set_state(ObserverState.GONE)
            return
        if self._state == ObserverState.BUSY and self._last_event_ts is not None:
            silence = time.time() - self._last_event_ts
            if silence > self._expected_silence:
                pending_kinds = set(self._pending_tools.values())
                if pending_kinds & GATE_TOOL_KINDS:
                    self._set_state(ObserverState.GATE)
                else:
                    self._set_state(ObserverState.STUCK)

    def reset(self, new_pid: int, session_dir: str = None):
        self.pid = new_pid
        self._state = ObserverState.STARTING
        self._since = time.time()
        self._last_event_type = None
        self._last_event_ts = None
        self._pending_tools.clear()
        self._expected_silence = self._default_silence
        self._turn_event_count = 0
        self._retry_attempt = None
        self._retry_reason = None
        self._doom_loop = False
        self._unknown_event = None
        self._exit_code = None
        self._pid_proc_state = None

    def apply_many(self, events: list) -> None:
        for ev in events:
            if isinstance(ev, ActivityEvent):
                self.apply(ev)

    def state_dict(self) -> dict:
        now = time.time()
        return {
            "state": self._state.value,
            "since": self._since,
            "last_event_type": self._last_event_type,
            "last_event_ts": self._last_event_ts,
            "retry_attempt": self._retry_attempt,
            "retry_reason": self._retry_reason,
            "exit_code": self._exit_code,
            "pid": self.pid,
            "pid_proc_state": self._pid_proc_state,
            "unknown_event": self._unknown_event,
            "doom_loop": self._doom_loop,
            "turn_event_count": self._turn_event_count,
            "pending_tools": (
                {tid: kind for tid, kind in self._pending_tools.items()}
                if self._pending_tools
                else None
            ),
            "model_id": self._model_id,
            "reasoning_effort": self._reasoning_effort,
            "activity": self._activity,
            "written_at": now,
            "expires_at": now + STATE_TTL,
        }

    def write_state_file(self, path: str):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state_dict(), f)
        os.rename(tmp, path)

    @staticmethod
    def read_state_file(path: str) -> Optional[dict]:
        try:
            with open(path) as f:
                state = json.load(f)
            if time.time() > state.get("expires_at", 0):
                return None
            return state
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
