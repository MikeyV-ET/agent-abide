"""Claude backend → aa.stream adapter (stub).

Mirror grok.py:
  - locate ~/.claude/projects/... session jsonl
  - map native line → build_event(...)
  - tail_once → append_hot_events

Astro (Sixel) uses backend=claude; wire tail when session format is stable.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional


NATIVE_CLAUDE = "claude.session_jsonl.v1"  # placeholder schema name


def find_live_session(agent_home: Path, session_id: Optional[str] = None) -> Optional[Path]:
    """Locate Claude session transcript. TODO: use api/session_locator."""
    return None


def map_claude_event(obj: dict) -> tuple:
    """Return (class_, phase, role, body) — TODO."""
    return ("unknown", "none", None, None)


def tail_claude_once(
    agent_home: Path,
    agent: str,
    *,
    session_id: Optional[str] = None,
    source: Optional[Path] = None,
    max_lines: Optional[int] = None,
    max_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """Ingest new Claude session bytes into hot.jsonl. Stub."""
    return {
        "status": "not_implemented",
        "error": "claude stream adapter stub — implement map + tail like stream_adapters.grok",
        "agent": agent,
        "native_schema": NATIVE_CLAUDE,
    }
