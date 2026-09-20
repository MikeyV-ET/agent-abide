"""TUI history SoR: prefer asdaaas/history/hot.jsonl (aa.stream).

P0 cutover helper — parse + path resolve. Live tail wiring is P1.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator, Optional

from aa_stream_parser import is_aa_stream_path, parse_aa_stream


def agent_history_dir(agent_home: Path) -> Path:
    return Path(agent_home) / "asdaaas" / "history"


def hot_jsonl_path(agent_home: Path) -> Path:
    return agent_history_dir(agent_home) / "hot.jsonl"


def updates_jsonl_candidates(agent_home: Path) -> list[Path]:
    """Legacy grok session updates paths (best-effort)."""
    home = Path(agent_home)
    cands = []
    # common: asdaaas may symlink or store session under .grok
    for p in [
        home / "asdaaas" / "updates.jsonl",
        home / "updates.jsonl",
    ]:
        if p.exists():
            cands.append(p)
    return cands


def resolve_history_source(agent_home: Path, prefer: Optional[str] = None) -> tuple[str, Path]:
    """Return (kind, path) kind in hot|updates|none.

    prefer: env TUI_HISTORY_SOURCE or arg: hot|updates|auto
    """
    prefer = (prefer or os.environ.get("TUI_HISTORY_SOURCE") or "auto").lower()
    hot = hot_jsonl_path(agent_home)
    if prefer == "hot":
        return ("hot", hot) if hot.exists() else ("none", hot)
    if prefer == "updates":
        ups = updates_jsonl_candidates(agent_home)
        return ("updates", ups[0]) if ups else ("none", hot)
    # auto
    if hot.exists() and hot.stat().st_size > 0:
        return ("hot", hot)
    ups = updates_jsonl_candidates(agent_home)
    if ups:
        return ("updates", ups[0])
    return ("none", hot)


def entries_from_hot(path: Path) -> list[dict[str, Any]]:
    return list(parse_aa_stream(path))


def entry_to_tui_lines(entry: dict[str, Any]) -> list[str]:
    """Minimal paint lines for catch-up (P0)."""
    role = (entry.get("role") or entry.get("type") or "").lower()
    text = entry.get("content") or entry.get("text") or ""
    if isinstance(text, list):
        text = " ".join(
            (x.get("text") if isinstance(x, dict) else str(x)) for x in text
        )
    text = str(text).strip()
    if not text:
        return []
    if role in ("user", "human"):
        return [f"You: {text}"]
    if role in ("assistant", "agent"):
        return [text]
    if role in ("system", "control"):
        return [f"[{text}]"]
    return [text]
