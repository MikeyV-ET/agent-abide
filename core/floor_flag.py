"""Room floor (hand-raise). Separate from operator_flag (tab color).

Agents: {"action": "flag", "status": "floor", "room": "#gen4"}
        {"action": "flag", "status": "unfloor"}
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

FLOOR_NAME = "floor_flag.json"


def floor_path(asdaaas_dir: Path | str) -> Path:
    return Path(asdaaas_dir) / FLOOR_NAME


def write_floor(
    asdaaas_dir: Path | str,
    room: str,
    note: str = "",
    *,
    agent: str = "",
) -> Path:
    p = floor_path(asdaaas_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    room = (room or "").strip()
    if room and not room.startswith("#"):
        room = "#" + room
    payload = {
        "raised": True,
        "room": room.lower() if room else "",
        "note": note or "",
        "agent": agent,
        "ts": time.time(),
    }
    p.write_text(json.dumps(payload, indent=2) + "\n")
    return p


def clear_floor(asdaaas_dir: Path | str) -> None:
    p = floor_path(asdaaas_dir)
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def read_floor(asdaaas_dir: Path | str) -> Optional[dict[str, Any]]:
    p = floor_path(asdaaas_dir)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("raised"):
        return None
    return data


def raised_in_room(channel: str, members: list[str], asdaaas_dir_for) -> list[str]:
    """Member names with a raised floor for this channel, oldest first."""
    want = (channel or "").strip().lower()
    if want and not want.startswith("#"):
        want = "#" + want
    ranked: list[tuple[float, str]] = []
    for name in members:
        try:
            d = read_floor(asdaaas_dir_for(name))
        except Exception:
            d = None
        if not d:
            continue
        room = str(d.get("room") or "").lower()
        if want and room and room != want:
            continue
        ranked.append((float(d.get("ts") or 0), name))
    ranked.sort()
    return [n for _ts, n in ranked]
