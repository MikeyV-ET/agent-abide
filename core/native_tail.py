"""Single-file JSONL byte tail: complete lines only, one cursor.

Used as the shared ear for a native flight-recorder file (e.g. Grok
updates.jsonl). Incomplete trailing lines do not advance the offset.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


@dataclass
class LineRecord:
    """One complete JSONL line."""

    offset: int  # byte offset of line start
    end_offset: int  # byte offset after newline
    text: str  # line without trailing newline


@dataclass
class LineBatch:
    path: Path
    start_offset: int
    end_offset: int
    records: List[LineRecord] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.records)


class JsonlByteTail:
    """Checkpointed complete-line reader for one path."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.offset = 0
        self._fp = None

    def close(self) -> None:
        if self._fp is not None:
            try:
                self._fp.close()
            except Exception:
                pass
            self._fp = None

    def seek(self, offset: int) -> None:
        self.offset = max(0, int(offset))
        if self._fp is not None:
            try:
                self._fp.seek(self.offset)
            except Exception:
                self.close()

    def seek_end(self) -> int:
        self._ensure_open()
        if self._fp is None:
            return self.offset
        self._fp.seek(0, os.SEEK_END)
        self.offset = self._fp.tell()
        return self.offset

    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def behind(self) -> int:
        sz = self.size()
        if self.offset > sz:
            return sz  # truncated — caller should reset
        return max(0, sz - self.offset)

    def _ensure_open(self) -> bool:
        if self._fp is not None:
            return True
        if not self.path.exists():
            return False
        try:
            self._fp = open(self.path, "rb")
            sz = self.path.stat().st_size
            if self.offset > sz:
                self.offset = 0
            self._fp.seek(self.offset)
            return True
        except OSError:
            self._fp = None
            return False

    def read_batch(
        self,
        *,
        max_lines: Optional[int] = None,
        max_bytes: Optional[int] = None,
    ) -> LineBatch:
        """Read newly complete lines since offset. Does not parse JSON."""
        start = self.offset
        batch = LineBatch(path=self.path, start_offset=start, end_offset=start)
        if not self._ensure_open() or self._fp is None:
            return batch
        # truncation
        try:
            sz = self.path.stat().st_size
            if self.offset > sz:
                self.offset = 0
                self._fp.seek(0)
                start = 0
                batch.start_offset = 0
        except OSError:
            return batch

        bytes_read = 0
        lines = 0
        while True:
            if max_lines is not None and lines >= max_lines:
                break
            if max_bytes is not None and bytes_read >= max_bytes:
                break
            line_off = self._fp.tell()
            raw = self._fp.readline()
            if not raw:
                break
            if not raw.endswith(b"\n"):
                # incomplete — leave offset at line start
                self._fp.seek(line_off)
                break
            text = raw.decode("utf-8", errors="replace").rstrip("\n\r")
            end_off = self._fp.tell()
            batch.records.append(
                LineRecord(offset=line_off, end_offset=end_off, text=text)
            )
            self.offset = end_off
            bytes_read += len(raw)
            lines += 1
        batch.end_offset = self.offset
        return batch
