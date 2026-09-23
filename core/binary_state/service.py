"""ObserverService + InProcessObserver (orchestration).

Default edge is Grok (updates.jsonl). Claude edge swaps observer class later.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from typing import Optional

from binary_state.data import load_known_types, load_silence_windows
from binary_state.grok import GrokBinaryStateObserver
from binary_state.tail import UpdatesJSONLTailer
from binary_state.types import DEFAULT_SILENCE_WINDOW, ObserverState

# Back-compat name inside this module
BinaryStateObserver = GrokBinaryStateObserver

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
            # observer_data lives in core/ next to this package
            data_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "observer_data",
            )

        # Load configuration from data files
        known_types = load_known_types(data_dir)
        tool_windows, event_windows, default_window, self._p95_events = \
            load_silence_windows(data_dir)

        # Override the module-level default
        import binary_state.types as _types
        _types.DEFAULT_SILENCE_WINDOW = default_window

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
# InProcessObserver -- async wrapper for in-process use
# ============================================================================

class InProcessObserver:
    """
    Async wrapper that runs BinaryStateObserver as an asyncio task inside
    the asdaaas process. Replaces ObserverService (sidecar subprocess).

    Tails updates.jsonl for turn lifecycle events. Stdout events are fed
    directly via process_stdout_event() called from _process_stdout.

    Writes state file periodically for external consumers (dashboards).
    """

    STATE_WRITE_INTERVAL = 1.0  # seconds (relaxed from sidecar's 250ms)
    HEARTBEAT_INTERVAL = 0.25   # seconds

    def __init__(
        self,
        pid: int,
        session_dir: str,
        state_file: str,
        data_dir: str = None,
        native_bus=None,
    ):
        # Resolve data directory
        if data_dir is None:
            # observer_data lives in core/ next to this package
            data_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "observer_data",
            )

        # Load configuration
        known_types = load_known_types(data_dir)
        tool_windows, event_windows, default_window, self._p95_events = \
            load_silence_windows(data_dir)

        import binary_state.types as _types
        _types.DEFAULT_SILENCE_WINDOW = default_window

        # Create observer (the pure state machine)
        self.observer = BinaryStateObserver(
            pid=pid,
            known_types=known_types,
            silence_windows=tool_windows,
            event_silence_windows=event_windows,
        )

        # Live ear: shared GrokNativeBus (phase 3) or private UpdatesJSONLTailer
        updates_path = os.path.join(session_dir, "updates.jsonl")
        self._bus = native_bus
        self._tailer = None if native_bus is not None else UpdatesJSONLTailer(updates_path)
        self._state_file = state_file
        self._session_dir = session_dir
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._via = "bus" if native_bus is not None else "private_tail"

    # -- Public interface --

    @property
    def state(self) -> ObserverState:
        return self.observer.state

    def state_dict(self) -> dict:
        return self.observer.state_dict()

    def process_stdout_event(self, frame: dict):
        """Called from _process_stdout to feed stdout notifications."""
        self.observer.process_stdout_event(frame)

    def reset(self, new_pid: int, session_dir: str = None):
        """Reset for a new binary process after restart."""
        self.observer.reset(new_pid)
        if session_dir:
            self._session_dir = session_dir
            if self._bus is None:
                if self._tailer is not None:
                    self._tailer.close()
                updates_path = os.path.join(session_dir, "updates.jsonl")
                self._tailer = UpdatesJSONLTailer(updates_path)
        self._orient()
        self.observer.write_state_file(self._state_file)

    def start(self):
        """Start the observer as an asyncio task."""
        self._orient()
        self._running = True
        self._task = asyncio.get_event_loop().create_task(self._run())

    def stop(self):
        """Stop the observer task."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    # -- Internal --

    def _orient(self):
        """Scan tail of updates.jsonl to establish current state."""
        scan_size = self._p95_events
        max_retries = 3
        # Orientation always reads the file directly (backward scan). Live
        # path may still use the shared bus after attach.
        orient_tailer = self._tailer
        if orient_tailer is None:
            updates_path = os.path.join(self._session_dir, "updates.jsonl")
            orient_tailer = UpdatesJSONLTailer(updates_path)

        for attempt in range(max_retries):
            lines = orient_tailer.read_tail_lines(scan_size)
            if not lines:
                break

            frames = []
            for line in lines:
                try:
                    frames.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

            if not frames:
                break

            self.observer.orient_from_history(frames)

            if self.observer.state != ObserverState.STARTING:
                break

            scan_size *= 3
            self.observer._state = ObserverState.STARTING
            self.observer._last_event_type = None
            self.observer._last_event_ts = None
            self.observer._pending_tools.clear()
            self.observer._turn_event_count = 0

        if self._bus is not None:
            # Shared ear: attach occupancy at current bus tip (after catch-up)
            try:
                self._bus.catch_up()
            except Exception:
                pass
            self._bus.begin_occupancy_window()
        elif self._tailer is not None:
            self._tailer.seek_to_end()
        else:
            orient_tailer.seek_to_end()
            if self._bus is None:
                # keep a private live tailer if bus missing
                self._tailer = orient_tailer
        self.observer.write_state_file(self._state_file)

    def poll_once(self):
        """Feed any new tailed lines to the observer. Sync; safe to call often."""
        if self._bus is not None:
            for frame in self._bus.read_for_occupancy():
                self.observer.process_event(frame)
            return
        if self._tailer is None:
            return
        for line in self._tailer.read_new_lines():
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.observer.process_event(frame)

    async def _run(self):
        """Async event loop: tail updates.jsonl, heartbeat, write state."""
        last_write = 0.0

        try:
            while self._running:
                # Process new native events
                try:
                    self.poll_once()
                except Exception as e:
                    # Do not look like a quiet binary: log and keep heartbeat
                    # writing so TUI can see the observer is sick.
                    print(f"[observer] poll_once error: {type(e).__name__}: {e}")
                    try:
                        self.observer.write_state_file(self._state_file)
                    except Exception:
                        pass

                # Heartbeat: check process liveness + silence
                self.observer.check_heartbeat()

                # Write state file at reduced frequency
                now = time.time()
                if now - last_write >= self.STATE_WRITE_INTERVAL:
                    self.observer.write_state_file(self._state_file)
                    last_write = now

                # GONE: write final state, stop
                if self.observer.state == ObserverState.GONE:
                    self.observer.write_state_file(self._state_file)
                    break

                # Yield to event loop
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[observer] _run crashed: {type(e).__name__}: {e}")
            raise
        finally:
            if self._bus is not None:
                try:
                    self._bus.end_occupancy_window()
                except Exception:
                    pass
            if self._tailer is not None:
                self._tailer.close()
            # Write final state on shutdown
            try:
                self.observer.write_state_file(self._state_file)
            except Exception:
                pass


class ClaudeInProcessObserver(InProcessObserver):
    """Claude edge: tail the Claude Code session jsonl instead of updates.jsonl.

    Claude has no ``updates.jsonl`` and no stdout JSON-RPC plane. Its native
    record is the session transcript under
    ``~/.claude/projects/<escaped-cwd>/<session-id>.jsonl``, which is also what
    the AA hot-stream ingest reads (separate offsets — this tailer is live, hot
    ingest is checkpointed).

    Everything after "line → dict" is shared with the grok path: same tailer,
    same poll/heartbeat/state-file loop, same machine.
    """

    def __init__(
        self,
        pid: int,
        session_file: str,
        state_file: str,
        data_dir: str = None,
        on_model_id=None,
    ):
        # Deliberately not calling super().__init__: it builds the grok observer
        # and points a tailer at updates.jsonl.
        if data_dir is None:
            data_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "observer_data",
            )

        tool_windows, event_windows, default_window, self._p95_events = \
            load_silence_windows(data_dir)

        import binary_state.types as _types
        _types.DEFAULT_SILENCE_WINDOW = default_window

        from binary_state.claude import ClaudeBinaryStateObserver

        self.observer = ClaudeBinaryStateObserver(
            pid=pid,
            silence_windows=tool_windows,
            event_silence_windows=event_windows,
            on_model_id=on_model_id,
        )
        self._tailer = UpdatesJSONLTailer(session_file)
        self._session_file = session_file
        self._session_dir = os.path.dirname(session_file)
        self._state_file = state_file
        self._running = False
        self._task: Optional[asyncio.Task] = None
        # Skipping super().__init__ means every attribute the base sets must be
        # set here too. Claude has no grok native bus -- it tails its own
        # transcript -- so the bus is None and the route is a private tail.
        # 2b67bc4 added self._bus to the base and a `self._bus is not None`
        # check to poll_once; this subclass did not have it, and poll_once
        # raised AttributeError on every tick. Live that froze the Claude
        # binary state at the values it held when the process started.
        self._bus = None
        self._via = "private_tail"

    def orient(self):
        """Read the transcript tail to establish state. Sync; no event loop."""
        self._orient()

    def process_stdout_event(self, frame: dict):
        """No-op: Claude has no stdout notification plane."""
        return None

    def reset(self, new_pid: int, session_dir: str = None, session_file: str = None):
        """Re-point at a new process, and a new transcript if the session moved."""
        self.observer.reset(new_pid)
        target = session_file or (
            os.path.join(session_dir, os.path.basename(self._session_file))
            if session_dir
            else None
        )
        if target:
            self._session_file = target
            self._session_dir = os.path.dirname(target)
            self._tailer.close()
            self._tailer = UpdatesJSONLTailer(target)
        self._orient()

    def _orient(self):
        """Replay the tail of the transcript, then follow from the end.

        The grok version retries with a widening scan and resets observer
        internals between attempts; those attributes live on the grok observer,
        not this one. A single pass is enough here because Claude's transcript
        carries explicit turn boundaries (stop_reason=end_turn), so a short tail
        already says whether a turn is open.
        """
        frames = []
        for line in self._tailer.read_tail_lines(self._p95_events):
            try:
                frames.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        if frames:
            self.observer.orient_from_history(frames)

        self._tailer.seek_to_end()
        self.observer.write_state_file(self._state_file)


# ============================================================================
# CLI entry point
# ============================================================================

