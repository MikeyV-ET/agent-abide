"""Live IRC round-robin state for TUI (order, seat, flags).

Adapter writes; TUI paints. Not the roster (see irc_rooms.json).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

DEFAULT_PATH = Path.home() / "agents" / "config" / "irc_round.json"


def _norm(channel: str) -> str:
    name = (channel or "").strip()
    if not name:
        return ""
    body = name.split()[0].lstrip("#").lower()
    return ("#" + body) if body else ""


def load(path: Path | None = None) -> dict:
    p = Path(path or DEFAULT_PATH)
    try:
        data = json.loads(p.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def read_round(channel: str, path: Path | None = None) -> dict | None:
    ch = _norm(channel)
    if not ch:
        return None
    row = load(path).get(ch)
    return row if isinstance(row, dict) else None


def write_round(channel: str, *, order: list[str], seat: str, flags: list[str], path: Path | None = None) -> None:
    ch = _norm(channel)
    if not ch:
        return
    p = Path(path or DEFAULT_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = load(p)
    data[ch] = {
        "order": list(order),
        "seat": seat,
        "flags": list(flags),
        "ts": time.time(),
    }
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(p)


def clear_round(channel: str, path: Path | None = None) -> None:
    ch = _norm(channel)
    if not ch:
        return
    p = Path(path or DEFAULT_PATH)
    data = load(p)
    if ch not in data:
        return
    del data[ch]
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(p)


def format_round_line(row: dict | None) -> str:
    if not row:
        return ""
    order = [str(x) for x in (row.get("order") or []) if str(x).strip()]
    flags = {str(x) for x in (row.get("flags") or [])}
    seat = str(row.get("seat") or "")
    if not order:
        return ""
    seq = " → ".join(f"{n}*" if n == seat else n for n in order)
    open_flags = [n for n in order if n in flags]
    flag_s = ",".join(open_flags) if open_flags else "none"
    return f"rr {seq}  flags {flag_s}"
