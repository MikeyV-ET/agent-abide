"""TUI multiway chat: one operator line → several agents' tui inboxes.

Template for thiasai rooms. Tab id is ``#mw-Name-Name``. Not IRC.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable


def parse_members(arg: str) -> list[str]:
    parts = re.split(r"[\s,+/]+", (arg or "").strip())
    names = []
    seen = set()
    for p in parts:
        p = p.strip().lstrip("#")
        if p.lower().startswith("mw-"):
            p = p[3:]
        if not p:
            continue
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(p)
    return names


def tab_id(members: Iterable[str]) -> str:
    names = [m for m in members if m]
    if not names:
        raise ValueError("need at least one member")
    return "#mw-" + "-".join(names)


def members_from_tab(tab: str) -> list[str]:
    s = str(tab or "")
    if s.startswith("#mw-"):
        s = s[4:]
    return parse_members(s.replace("-", " "))


def is_mw_tab(tab: str | None) -> bool:
    return str(tab or "").startswith("#mw-")


def new_jsonl_records(path: Path, offset: int) -> tuple[list[dict], int]:
    """Read new JSONL objects from byte offset. Returns (rows, new_offset)."""
    if not path.is_file():
        return [], offset
    size = path.stat().st_size
    if offset > size:
        offset = 0
    rows = []
    with path.open("r", encoding="utf-8") as f:
        f.seek(offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if isinstance(o, dict):
                rows.append(o)
        new_off = f.tell()
    return rows, new_off
