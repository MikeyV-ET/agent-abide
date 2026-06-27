"""
binary_state_observer.py -- Binary State Observer (reference implementation).

Monitors grok binary state by processing updates.jsonl events and checking
process liveness. Reports state via atomic JSON file.

Spec: ~/agents/Trip/AA-architecture-audit/binary_state_observer_spec.md

This is a reference implementation written alongside the contract tests.
Sr may replace or extend this as needed.
"""

import enum
import json
import os
import time
from typing import Callable, Optional


class ObserverState(enum.Enum):
    STARTING = "STARTING"
    IDLE = "IDLE"
    BUSY = "BUSY"
    RETRYING = "RETRYING"
    STUCK = "STUCK"
    GONE = "GONE"
    UNKNOWN = "UNKNOWN"


# Default fallback silence window (seconds). From timing report: P99 = 30s.
DEFAULT_SILENCE_WINDOW = 60  # conservative: 2x P99

# Buffer multiplier for explicit timeouts (account for overhead)
TIMEOUT_BUFFER = 1.5


class BinaryStateObserver:
    """
    Core state machine for the binary state observer.

    Processes updates.jsonl events one at a time and maintains state.
    Process liveness is checked via check_heartbeat().
    """

    def __init__(
        self,
        pid: int,
        known_types: set[str],
        process_alive_fn: Callable[[int], bool] = None,
        silence_windows: dict[str, float] = None,
    ):
        self.pid = pid
        self._known_types = known_types
        self._process_alive_fn = process_alive_fn or self._default_process_check
        self._silence_windows = silence_windows or {}

        # State
        self._state = ObserverState.STARTING
        self._since = time.time()
        self._last_event_type: Optional[str] = None
        self._last_event_ts: Optional[float] = None
        self._pending_tools: set[str] = set()
        self._expected_silence: float = DEFAULT_SILENCE_WINDOW
        self._turn_event_count: int = 0

        # Retry metadata
        self._retry_attempt: Optional[int] = None
        self._retry_reason: Optional[str] = None

        # Flags
        self._doom_loop: bool = False
        self._unknown_event: Optional[str] = None

        # GONE metadata
        self._exit_code: Optional[int] = None

    @staticmethod
    def _default_process_check(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    # -- Properties --

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
        return len(self._pending_tools) > 0

    @property
    def turn_event_count(self) -> int:
        return self._turn_event_count

    # -- Core event processing --

    def _set_state(self, new_state: ObserverState):
        if new_state != self._state:
            self._state = new_state
            self._since = time.time()

    def _extract_event_type(self, frame: dict) -> str:
        """Extract sessionUpdate type from an updates.jsonl frame."""
        try:
            return frame["params"]["update"]["sessionUpdate"]
        except (KeyError, TypeError):
            return "unknown"

    def _extract_update(self, frame: dict) -> dict:
        """Extract the full update dict from a frame."""
        try:
            return frame["params"]["update"]
        except (KeyError, TypeError):
            return {}

    def process_event(self, frame: dict):
        """Process a single updates.jsonl event and update state."""
        event_type = self._extract_event_type(frame)
        update = self._extract_update(frame)
        now = time.time()

        self._last_event_type = event_type
        self._last_event_ts = now

        # Unknown type check
        if event_type not in self._known_types:
            self._set_state(ObserverState.UNKNOWN)
            self._unknown_event = event_type
            return

        # Clear unknown on recognized event
        if self._state == ObserverState.UNKNOWN:
            self._unknown_event = None

        # State transitions per spec pseudocode
        if event_type == "user_message_chunk":
            self._set_state(ObserverState.BUSY)
            self._pending_tools.clear()
            self._turn_event_count = 1
            self._doom_loop = False
            self._retry_attempt = None
            self._retry_reason = None
            return

        self._turn_event_count += 1

        if event_type == "turn_completed":
            self._set_state(ObserverState.IDLE)
            self._pending_tools.clear()
            self._turn_event_count = 0

        elif event_type == "retry_state":
            retry_type = update.get("type", "")
            if retry_type == "retrying":
                self._set_state(ObserverState.RETRYING)
                self._retry_attempt = update.get("attempt")
                self._retry_reason = update.get("reason")
            elif retry_type == "failed":
                self._set_state(ObserverState.BUSY)

        elif event_type == "tool_call":
            tool_id = update.get("toolCallId")
            if tool_id:
                self._pending_tools.add(tool_id)
            self._expected_silence = self._compute_expected_silence(update)
            # Stay BUSY (or transition to BUSY from RETRYING/UNKNOWN)
            if self._state not in (ObserverState.BUSY,):
                self._set_state(ObserverState.BUSY)

        elif event_type == "tool_call_update":
            tool_id = update.get("toolCallId")
            status = update.get("status", "")
            if tool_id and status in ("completed", "failed"):
                self._pending_tools.discard(tool_id)
            # Stay BUSY
            if self._state not in (ObserverState.BUSY,):
                self._set_state(ObserverState.BUSY)

        elif event_type == "doom_loop_detected":
            self._doom_loop = True

        elif event_type in ("agent_message_chunk", "agent_thought_chunk"):
            # These indicate the binary is producing output — BUSY
            if self._state in (ObserverState.RETRYING, ObserverState.UNKNOWN,
                               ObserverState.STARTING):
                self._set_state(ObserverState.BUSY)

    def _compute_expected_silence(self, update: dict) -> float:
        """Compute expected silence window from tool call context."""
        tool_name = update.get("title", "")
        raw_input = update.get("rawInput", "")

        # Try to parse explicit timeout from rawInput
        if raw_input:
            try:
                params = json.loads(raw_input) if isinstance(raw_input, str) else raw_input
                # timeout_ms (wait_commands_or_subagents)
                if "timeout_ms" in params:
                    return (params["timeout_ms"] / 1000) * TIMEOUT_BUFFER
                # timeout (generic)
                if "timeout" in params:
                    timeout_val = params["timeout"]
                    if isinstance(timeout_val, (int, float)):
                        return timeout_val * TIMEOUT_BUFFER
            except (json.JSONDecodeError, TypeError):
                pass

        # Per-tool historical distribution
        if tool_name in self._silence_windows:
            return self._silence_windows[tool_name]

        return DEFAULT_SILENCE_WINDOW

    # -- Heartbeat --

    def check_heartbeat(self):
        """Check process liveness and silence windows. Call periodically."""
        # Process liveness check
        if not self._process_alive_fn(self.pid):
            self._set_state(ObserverState.GONE)
            return

        # STUCK check: only when BUSY and silence exceeds expected window
        if self._state == ObserverState.BUSY and self._last_event_ts is not None:
            silence = time.time() - self._last_event_ts
            if silence > self._expected_silence:
                self._set_state(ObserverState.STUCK)

    # -- Orientation --

    def orient_from_history(self, events: list[dict]):
        """Process a list of historical events to establish current state."""
        if not events:
            return  # stays STARTING

        for event in events:
            self.process_event(event)

    # -- State file output --

    def state_dict(self) -> dict:
        """Return the state as a dict suitable for writing to JSON."""
        return {
            "state": self._state.value,
            "since": self._since,
            "last_event_type": self._last_event_type,
            "last_event_ts": self._last_event_ts,
            "retry_attempt": self._retry_attempt,
            "retry_reason": self._retry_reason,
            "exit_code": self._exit_code,
            "pid": self.pid,
            "unknown_event": self._unknown_event,
            "doom_loop": self._doom_loop,
            "turn_event_count": self._turn_event_count,
        }

    def write_state_file(self, path: str):
        """Write state atomically (write tmp, rename)."""
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state_dict(), f)
        os.rename(tmp, path)
