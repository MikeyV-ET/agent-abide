"""hot.jsonl tail offset policy.

Prune is archive-then-keep-tail. If the file shrinks under the read cursor,
jump to the new EOF. Do not rewind to 0 (that remounts the remaining file,
re-pushes every ephact, and mount-before's unparented tool panels).
"""
from __future__ import annotations


def offset_after_stat(offset: int, size: int) -> int:
    """Return the next read offset given previous offset and current file size."""
    if size < 0:
        return 0
    if size < offset:
        return size
    return offset
