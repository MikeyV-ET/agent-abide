"""
binary_state_observer.py -- Binary State Observer.

Monitors grok binary state by tailing updates.jsonl events and checking
process liveness via /proc/[pid]/stat. Reports state via atomic JSON file.

Spec: ~/agents/Trip/AA-architecture-audit/binary_state_observer_spec.md
Data: ~/projects/agent-abide/core/observer_data/

Usage:
    python3 binary_state_observer.py --pid PID --session-dir DIR --state-file PATH

Components:
    BinaryStateObserver  -- Core state machine (event processing, heartbeat)
    UpdatesJSONLTailer   -- Efficient file tailing for updates.jsonl
    ObserverService      -- Main event loop (tail + heartbeat + state file writes)
"""

import argparse
import enum
import json
import os
import signal
import sys
import time
from pathlib import Path
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
        event_silence_windows: dict[str, float] = None,
    ):
        self.pid = pid
        self._known_types = known_types
        self._process_alive_fn = process_alive_fn or self._default_process_check
        self._silence_windows = silence_windows or {}  # by tool name
        self._event_silence_windows = event_silence_windows or {}  # by preceding event type

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
        self._pid_proc_state: Optional[str] = None

    @staticmethod
    def _default_process_check(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def read_proc_state(pid: int) -> Optional[str]:
        """Read process state char from /proc/[pid]/stat. Returns R/S/D/Z/T or None."""
        try:
            with open(f"/proc/{pid}/stat") as f:
                # Format: pid (comm) state ...
                line = f.read()
                # Find state char after the closing paren of comm field
                close_paren = line.rfind(")")
                if close_paren >= 0 and close_paren + 2 < len(line):
                    return line[close_paren + 2]
        except (FileNotFoundError, PermissionError, OSError):
            pass
        return None

    @staticmethod
    def read_exit_code(pid: int) -> Optional[int]:
        """Try to reap exit code via waitpid (non-blocking). Returns None if not our child."""
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                if os.WIFEXITED(status):
                    return os.WEXITSTATUS(status)
                elif os.WIFSIGNALED(status):
                    return 128 + os.WTERMSIG(status)
        except ChildProcessError:
            pass
        return None

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

        # Update expected silence based on the event we just saw
        if event_type != "tool_call":  # tool_call sets its own via _compute_expected_silence
            self._expected_silence = self._event_silence_windows.get(
                event_type, DEFAULT_SILENCE_WINDOW
            )

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
        # Read proc state
        self._pid_proc_state = self.read_proc_state(self.pid)

        # Process liveness check
        if not self._process_alive_fn(self.pid):
            self._exit_code = self.read_exit_code(self.pid)
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
            "pid_proc_state": self._pid_proc_state,
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


# ============================================================================
# UpdatesJSONLTailer -- efficient file tailing
# ============================================================================

class UpdatesJSONLTailer:
    """
    Tail updates.jsonl line by line. Handles file not yet existing,
    file truncation, and efficient seeking.
    """

    def __init__(self, path: str):
        self._path = path
        self._file = None
        self._pos = 0

    def _open(self):
        """Open the file if it exists, seek to tracked position."""
        if self._file is not None:
            return True
        if not os.path.exists(self._path):
            return False
        try:
            self._file = open(self._path, "r")
            self._file.seek(self._pos)
            return True
        except OSError:
            return False

    def seek_to_end(self):
        """Position at end of file. Used after orientation completes."""
        if not self._open():
            return
        self._file.seek(0, 2)  # SEEK_END
        self._pos = self._file.tell()

    def read_tail_lines(self, n: int) -> list[str]:
        """Read last N lines of the file. Used for startup orientation."""
        if not os.path.exists(self._path):
            return []
        try:
            with open(self._path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size == 0:
                    return []
                # Read chunks from end to find N newlines
                chunk_size = min(8192, size)
                lines = []
                pos = size
                buf = b""
                while pos > 0 and len(lines) < n + 1:
                    read_size = min(chunk_size, pos)
                    pos -= read_size
                    f.seek(pos)
                    buf = f.read(read_size) + buf
                    lines = buf.split(b"\n")
                # Return last N non-empty lines
                result = [l.decode("utf-8", errors="replace")
                          for l in lines if l.strip()]
                return result[-n:]
        except OSError:
            return []

    def read_new_lines(self) -> list[str]:
        """Read any new complete lines since last read. Non-blocking."""
        if not self._open():
            return []
        # Check for truncation (file smaller than our position)
        try:
            current_size = os.path.getsize(self._path)
            if current_size < self._pos:
                # File was truncated/replaced — reopen
                self._file.close()
                self._file = None
                self._pos = 0
                if not self._open():
                    return []
        except OSError:
            return []

        lines = []
        while True:
            line = self._file.readline()
            if not line:
                break
            if line.endswith("\n"):
                lines.append(line.rstrip("\n"))
                self._pos = self._file.tell()
            else:
                # Partial line — seek back, wait for completion
                self._file.seek(self._pos)
                break
        return lines

    def close(self):
        if self._file:
            self._file.close()
            self._file = None


# ============================================================================
# Data loading
# ============================================================================

def load_known_types(data_dir: str) -> set[str]:
    """Load known event types from data file."""
    path = os.path.join(data_dir, "known_types.json")
    with open(path) as f:
        data = json.load(f)
    return set(data["types"])


def load_silence_windows(data_dir: str) -> tuple[dict[str, float], dict[str, float], float, int]:
    """Load silence windows from data file.

    Returns (by_tool_name, by_preceding_event, default, p95_events_per_turn).
    """
    path = os.path.join(data_dir, "silence_windows.json")
    with open(path) as f:
        data = json.load(f)
    return (
        data.get("by_tool_name", {}),
        data.get("by_preceding_event", {}),
        data.get("default", 60.0),
        data.get("orientation", {}).get("p95_events_per_turn", 200),
    )


# ============================================================================
# ObserverService -- main event loop
# ============================================================================

class ObserverService:
    """
    Main service: tails updates.jsonl, checks process, writes state file.

    Lifecycle:
        1. orient() — read tail of updates.jsonl to establish current state
        2. run()    — event loop: read new events, heartbeat, write state
    """

    HEARTBEAT_INTERVAL = 0.25  # seconds, matches asdaaas poll interval

    def __init__(
        self,
        pid: int,
        session_dir: str,
        state_file: str,
        data_dir: str = None,
    ):
        self.pid = pid
        self.session_dir = session_dir
        self.state_file = state_file
        self._running = False

        # Resolve data directory
        if data_dir is None:
            data_dir = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "observer_data"
            )

        # Load configuration from data files
        known_types = load_known_types(data_dir)
        tool_windows, event_windows, default_window, self._p95_events = \
            load_silence_windows(data_dir)

        # Override the module-level default
        global DEFAULT_SILENCE_WINDOW
        DEFAULT_SILENCE_WINDOW = default_window

        # Create observer
        self.observer = BinaryStateObserver(
            pid=pid,
            known_types=known_types,
            silence_windows=tool_windows,
            event_silence_windows=event_windows,
        )

        # Create tailer
        updates_path = os.path.join(session_dir, "updates.jsonl")
        self._tailer = UpdatesJSONLTailer(updates_path)

    def orient(self):
        """
        Startup orientation: scan backward using P95 of events-per-turn.
        If we can't determine state, retry with more lines.
        """
        scan_size = self._p95_events
        max_retries = 3

        for attempt in range(max_retries):
            lines = self._tailer.read_tail_lines(scan_size)
            if not lines:
                break  # No history — stay STARTING

            frames = []
            for line in lines:
                try:
                    frames.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

            if not frames:
                break

            self.observer.orient_from_history(frames)

            # If we found a turn boundary, we're oriented
            if self.observer.state != ObserverState.STARTING:
                break

            # Didn't find a turn boundary — scan more
            scan_size *= 3
            # Reset observer for retry
            self.observer._state = ObserverState.STARTING
            self.observer._last_event_type = None
            self.observer._last_event_ts = None
            self.observer._pending_tools.clear()
            self.observer._turn_event_count = 0

        # Position tailer at end for live tailing
        self._tailer.seek_to_end()

        # Write initial state
        self.observer.write_state_file(self.state_file)

    def _process_new_events(self) -> int:
        """Read and process any new events. Returns count processed."""
        lines = self._tailer.read_new_lines()
        count = 0
        for line in lines:
            try:
                frame = json.loads(line)
                self.observer.process_event(frame)
                count += 1
            except json.JSONDecodeError:
                continue
        return count

    def run(self):
        """Main event loop. Runs until SIGTERM/SIGINT or process GONE."""
        self._running = True

        def handle_signal(signum, frame):
            self._running = False

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        last_write = 0.0

        while self._running:
            # Process new events
            self._process_new_events()

            # Heartbeat: check process liveness + silence
            self.observer.check_heartbeat()

            # Write state file at heartbeat interval
            now = time.time()
            if now - last_write >= self.HEARTBEAT_INTERVAL:
                self.observer.write_state_file(self.state_file)
                last_write = now

            # If process is gone, write final state and exit
            if self.observer.state == ObserverState.GONE:
                self.observer.write_state_file(self.state_file)
                break

            # Sleep briefly to avoid busy-loop, but less than heartbeat
            time.sleep(0.05)

        self._tailer.close()

    def stop(self):
        """Signal the event loop to stop."""
        self._running = False


# ============================================================================
# CLI entry point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Binary State Observer — monitors grok binary state"
    )
    parser.add_argument("--pid", type=int, required=True,
                        help="PID of the grok binary process to monitor")
    parser.add_argument("--session-dir", required=True,
                        help="Path to the session directory containing updates.jsonl")
    parser.add_argument("--state-file", required=True,
                        help="Path to write the atomic state JSON file")
    parser.add_argument("--data-dir", default=None,
                        help="Path to observer data directory (default: ./observer_data/)")

    args = parser.parse_args()

    # Validate session dir exists
    if not os.path.isdir(args.session_dir):
        print(f"Error: session directory does not exist: {args.session_dir}",
              file=sys.stderr)
        sys.exit(1)

    # Ensure state file directory exists
    state_dir = os.path.dirname(args.state_file)
    if state_dir:
        os.makedirs(state_dir, exist_ok=True)

    service = ObserverService(
        pid=args.pid,
        session_dir=args.session_dir,
        state_file=args.state_file,
        data_dir=args.data_dir,
    )

    # Orient from existing history
    service.orient()

    # Run event loop
    service.run()


if __name__ == "__main__":
    main()
