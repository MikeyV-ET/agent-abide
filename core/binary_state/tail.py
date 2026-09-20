"""JSONL tailer (path-agnostic; used for grok updates.jsonl today)."""
from __future__ import annotations

import os
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

