"""
normalizers.py — Parse grok and claude session JSONL into a common message schema.

Common schema:
    {
        "id": int,              # sequential index in the session
        "timestamp": str,       # ISO8601
        "role": str,            # "user" | "assistant" | "thinking" | "tool_call" | "tool_result"
        "content": str,         # text content
        "raw_type": str,        # original backend-specific type
    }

Both backends write JSONL (one JSON object per line). This module provides:
    - parse_grok_line(line) -> dict | None
    - parse_claude_line(line) -> dict | None
    - read_messages(path, backend, last=None, before=None, limit=None) -> list[dict]
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Grok normalizer
# ---------------------------------------------------------------------------

_GROK_ROLE_MAP = {
    "user_message_chunk": "user",
    "agent_message_chunk": "assistant",
    "agent_thought_chunk": "thinking",
    "tool_call": "tool_call",
    "tool_call_update": "tool_result",
}


def parse_grok_line(raw: str) -> Optional[dict]:
    """Parse one line of grok updates.jsonl. Returns normalized dict or None if skipped."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None

    update = obj.get("params", {}).get("update", {})
    session_update = update.get("sessionUpdate", "")

    role = _GROK_ROLE_MAP.get(session_update)
    if role is None:
        return None

    # Extract text content
    content_obj = update.get("content", {})
    if isinstance(content_obj, dict):
        text = content_obj.get("text", "")
    elif isinstance(content_obj, list):
        # tool_call_update sends list of content blocks
        parts = []
        for block in content_obj:
            if isinstance(block, dict):
                inner = block.get("content", {})
                if isinstance(inner, dict):
                    parts.append(inner.get("text", ""))
                elif isinstance(inner, str):
                    parts.append(inner)
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(p for p in parts if p)
    elif isinstance(content_obj, str):
        text = content_obj
    else:
        text = str(content_obj)

    # For tool_call, include the tool name
    if role == "tool_call":
        tool_name = update.get("toolCallName", update.get("name", ""))
        if tool_name and text:
            text = f"[{tool_name}] {text}"
        elif tool_name:
            text = f"[{tool_name}]"

    ts_epoch = obj.get("timestamp", 0)
    ts_iso = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).isoformat()

    return {
        "timestamp": ts_iso,
        "role": role,
        "content": text,
        "raw_type": session_update,
    }


# ---------------------------------------------------------------------------
# Claude normalizer
# ---------------------------------------------------------------------------

_CLAUDE_ROLE_MAP = {
    "user": "user",
    "assistant": "assistant",
}


def parse_claude_line(raw: str) -> Optional[dict]:
    """Parse one line of claude session JSONL. Returns normalized dict or None if skipped."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None

    msg_type = obj.get("type", "")
    role = _CLAUDE_ROLE_MAP.get(msg_type)
    if role is None:
        return None

    # Extract text content
    message = obj.get("message", {})
    content = message.get("content", "")
    if isinstance(content, list):
        # Claude content blocks: [{"type": "text", "text": "..."}, ...]
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool: {block.get('name', '?')}]")
                elif block.get("type") == "tool_result":
                    parts.append(f"[result: {str(block.get('content', ''))[:200]}]")
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(parts)
    elif isinstance(content, str):
        text = content
    else:
        text = str(content)

    ts_raw = obj.get("timestamp", "")

    return {
        "timestamp": ts_raw,
        "role": role,
        "content": text,
        "raw_type": msg_type,
    }


# ---------------------------------------------------------------------------
# Unified reader
# ---------------------------------------------------------------------------

def read_messages(
    path: Path,
    backend: str,
    last: Optional[int] = None,
    before: Optional[int] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Read and normalize messages from a session file.

    Args:
        path: Path to the JSONL session file
        backend: "grok" or "claude"
        last: Return only the last N messages
        before: Return messages before this index (for pagination)
        limit: Max messages to return (used with before)

    Returns:
        List of normalized message dicts with sequential 'id' field added.
    """
    parser = parse_grok_line if backend == "grok" else parse_claude_line

    messages = []
    idx = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            msg = parser(line)
            if msg is not None:
                msg["id"] = idx
                messages.append(msg)
                idx += 1

    # Apply pagination
    if before is not None:
        messages = [m for m in messages if m["id"] < before]

    if last is not None:
        messages = messages[-last:]
    elif limit is not None:
        messages = messages[-limit:]

    return messages


class FileTailer:
    """Efficient file tailer that remembers byte offset between reads.

    Instead of re-reading the entire file, seeks to last known position
    and reads only new lines.
    """

    def __init__(self, path: Path, backend: str, start_id: int = -1):
        self.path = path
        self.backend = backend
        self.parser = parse_grok_line if backend == "grok" else parse_claude_line
        self.next_id = start_id + 1
        self.byte_offset = 0

        # If starting from a specific point, scan to find the byte offset
        if start_id >= 0:
            self._scan_to_id(start_id)
        else:
            # Start from end of file
            try:
                self.byte_offset = path.stat().st_size
                # Count all existing messages to set next_id
                msgs = read_messages(path, backend)
                self.next_id = (msgs[-1]["id"] + 1) if msgs else 0
            except (FileNotFoundError, IndexError):
                pass

    def _scan_to_id(self, target_id: int):
        """Scan file to find byte offset just after message with target_id."""
        msg_count = 0
        with open(self.path) as f:
            while True:
                pos = f.tell()
                line = f.readline()
                if not line:
                    self.byte_offset = pos
                    break
                line = line.strip()
                if not line:
                    continue
                msg = self.parser(line)
                if msg is not None:
                    if msg_count > target_id:
                        self.byte_offset = pos
                        self.next_id = msg_count
                        return
                    msg_count += 1
            self.byte_offset = f.tell()
            self.next_id = msg_count

    def poll(self) -> list[dict]:
        """Read new messages since last poll. Returns list of normalized dicts."""
        try:
            file_size = self.path.stat().st_size
        except FileNotFoundError:
            return []

        if file_size <= self.byte_offset:
            return []

        new_msgs = []
        with open(self.path) as f:
            f.seek(self.byte_offset)
            while True:
                line = f.readline()
                if not line:
                    break
                self.byte_offset = f.tell()
                line = line.strip()
                if not line:
                    continue
                msg = self.parser(line)
                if msg is not None:
                    msg["id"] = self.next_id
                    new_msgs.append(msg)
                    self.next_id += 1

        return new_msgs