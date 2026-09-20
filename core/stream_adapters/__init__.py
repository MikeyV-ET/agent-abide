"""Per-backend stream adapters: native session file → aa.stream events → hot.jsonl.

Contract:
  tail_<backend>_once(agent_home, agent, ...) -> stats
    reads native log since checkpoint
    maps each line via wrap_* → build_event
    append_hot_events(fs_dir, events)   # common writer in aa_stream

Grok: stream_adapters.grok
Claude: stream_adapters.claude (stub for Astro)
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional
from pathlib import Path


def tail_once_for_backend(
    backend: str,
    agent_home: Path,
    agent: str,
    **kwargs: Any,
) -> Dict[str, Any]:
    b = (backend or "grok").lower()
    if b in ("grok", "xai"):
        from stream_adapters.grok import tail_grok_once
        return tail_grok_once(agent_home, agent, **kwargs)
    if b in ("claude", "anthropic"):
        from stream_adapters.claude import tail_claude_once
        return tail_claude_once(agent_home, agent, **kwargs)
    return {"status": "error", "error": f"unknown backend {backend!r}"}
