"""Named IRC rooms and which agent nicks sit in each.

Source of truth: ~/agents/config/irc_rooms.json
  {"rooms": {"#standup": ["Trip-G"]}}
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_PATH = Path.home() / "agents" / "config" / "irc_rooms.json"


def _norm_channel(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    if not name.startswith("#"):
        name = "#" + name
    return name.split()[0][:32]


def load(path: Path | None = None) -> dict:
    p = Path(path or DEFAULT_PATH)
    try:
        data = json.loads(p.read_text())
    except Exception:
        return {"rooms": {}, "open_tabs": []}
    rooms = data.get("rooms") if isinstance(data, dict) else {}
    if not isinstance(rooms, dict):
        rooms = {}
    out = {}
    for ch, members in rooms.items():
        nch = _norm_channel(str(ch))
        if not nch:
            continue
        if isinstance(members, list):
            out[nch] = [str(m) for m in members if str(m).strip()]
        else:
            out[nch] = []
    raw_tabs = data.get("open_tabs") if isinstance(data, dict) else None
    tabs = []
    if isinstance(raw_tabs, list):
        for t in raw_tabs:
            nch = _norm_channel(str(t))
            if nch and nch not in tabs:
                tabs.append(nch)
    return {"rooms": out, "open_tabs": tabs, "path": str(p)}


def save(
    rooms: dict[str, list[str]],
    path: Path | None = None,
    open_tabs: list[str] | None = None,
) -> Path:
    p = Path(path or DEFAULT_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    try:
        existing = json.loads(p.read_text())
        if not isinstance(existing, dict):
            existing = {}
    except Exception:
        existing = {}
    clean = {}
    for ch, members in rooms.items():
        nch = _norm_channel(str(ch))
        if not nch:
            continue
        seen = []
        for m in members or []:
            s = str(m).strip()
            if s and s not in seen:
                seen.append(s)
        clean[nch] = seen
    existing["rooms"] = clean
    if open_tabs is not None:
        tabs = []
        for t in open_tabs:
            nch = _norm_channel(str(t))
            if nch and nch not in tabs:
                tabs.append(nch)
        existing["open_tabs"] = tabs
    p.write_text(json.dumps(existing, indent=2) + "\n")
    return p


def rooms(path: Path | None = None) -> dict[str, list[str]]:
    return load(path)["rooms"]


def add_room(channel: str, members: list[str] | None = None, path: Path | None = None) -> str:
    nch = _norm_channel(channel)
    cur = rooms(path)
    if nch not in cur:
        cur[nch] = list(members or [])
        save(cur, path)
    return nch


def add_member(channel: str, agent: str, path: Path | None = None) -> str:
    nch = _norm_channel(channel)
    cur = rooms(path)
    members = list(cur.get(nch) or [])
    if agent not in members:
        members.append(agent)
    cur[nch] = members
    save(cur, path)
    return nch


def room_awareness_cmd(channel: str, *, join: bool, mode: str = "doorbell") -> dict:
    """Command-queue payload so a room nick hears (or stops hearing) that channel."""
    nch = _norm_channel(channel)
    if join:
        return {"action": "awareness", "add": nch, "mode": mode}
    return {"action": "awareness", "remove": nch}


def remove_member(channel: str, agent: str, path: Path | None = None) -> str:
    nch = _norm_channel(channel)
    cur = rooms(path)
    members = [m for m in (cur.get(nch) or []) if m != agent]
    cur[nch] = members
    save(cur, path)
    return nch


def members_of(channel: str, path: Path | None = None) -> list[str]:
    return list(rooms(path).get(_norm_channel(channel)) or [])


def open_tabs(path: Path | None = None) -> list[str]:
    data = load(path)
    tabs = list(data.get("open_tabs") or [])
    if tabs:
        return tabs
    # Legacy file with no open_tabs: only rooms that have nicks
    return [ch for ch, ms in data["rooms"].items() if ms]


def set_open_tabs(tabs: list[str], path: Path | None = None) -> None:
    save(rooms(path), path=path, open_tabs=tabs)


def all_agents(path: Path | None = None) -> list[str]:
    seen = []
    for ms in rooms(path).values():
        for m in ms:
            if m not in seen:
                seen.append(m)
    return seen
