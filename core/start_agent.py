"""Create a family agent home and roster row. Never mint a session id."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

HISTORY_CONFIG = {
    "tail_grok": True,
    "use_inotify": True,
    "interval_ms": 1000,
    "comment": "owner=backend: first asdaaas start catch_up()s updates.jsonl into hot.jsonl.",
    "owner": "backend",
    "tail_stream": True,
}

GAZE = {"speech": {"target": "tui", "params": {}}, "thoughts": None}
AWARENESS = {
    "direct_attach": ["tui", "irc"],
    "control_watch": {},
    "notify_watch": [],
    "accept_from": ["*"],
    "default_doorbell": True,
    "doorbell_ttl": {"context": 1, "session": 2, "default": 3},
}


def _agents_dir(cfg: dict) -> Path:
    settings = cfg.get("settings") or {}
    return Path(settings.get("agents_dir") or Path.home() / "agents").expanduser()


def planned_home(family: str, name: str, *, agents_dir: Path) -> Path:
    fam = (family or "").strip()
    nm = (name or "").strip()
    if not fam or not nm:
        raise ValueError("family and name required")
    if fam.lower() in ("mikeyv", "flat"):
        return agents_dir / nm
    return agents_dir / fam / nm


def grok_session_dir(home: Path, session_id: str) -> Path:
    encoded = str(home.resolve()).replace("/", "%2F")
    return Path.home() / ".grok" / "sessions" / encoded / session_id


def find_session_dir(session_id: str, *, grok_sessions: Optional[Path] = None) -> Optional[Path]:
    root = grok_sessions or (Path.home() / ".grok" / "sessions")
    if not root.is_dir():
        return None
    hits = list(root.glob(f"*/{session_id}"))
    dirs = [p for p in hits if p.is_dir()]
    return dirs[0] if dirs else None


def write_history_config(home: Path) -> Path:
    hist = home / "asdaaas" / "history"
    hist.mkdir(parents=True, exist_ok=True)
    path = hist / "config.json"
    if not path.exists():
        path.write_text(json.dumps(HISTORY_CONFIG, indent=2) + "\n")
    return path


def ensure_home(family: str, name: str, *, agents_dir: Path) -> Path:
    home = planned_home(family, name, agents_dir=agents_dir)
    home.mkdir(parents=True, exist_ok=True)
    for rel in (
        "asdaaas/commands",
        "asdaaas/doorbells",
        "asdaaas/adapters/tui/inbox",
        "asdaaas/adapters/tui/outbox",
        "asdaaas/adapters/localmail/inbox",
        "asdaaas/adapters/localmail/outbox",
        "asdaaas/adapters/remind/inbox",
    ):
        (home / rel).mkdir(parents=True, exist_ok=True)
    gaze = home / "asdaaas" / "gaze.json"
    if not gaze.exists():
        gaze.write_text(json.dumps(GAZE, indent=2) + "\n")
    aw = home / "asdaaas" / "awareness.json"
    if not aw.exists():
        aw.write_text(json.dumps(AWARENESS, indent=2) + "\n")
    write_history_config(home)
    slug = name.lower().replace(" ", "-")
    agents_md = home / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(
            f"# {family}-{name}\n\nHome: `{home}`\nFamily: {family}\n"
        )
    nb = home / f"lab_notebook_{slug}.md"
    if not nb.exists():
        nb.write_text(f"# {name} lab notebook\n")
    notes = home / "notes_to_self.md"
    if not notes.exists():
        notes.write_text(f"# {name} — notes to self\n")
    return home


def upsert_roster(
    cfg_path: Path,
    *,
    name: str,
    family: str,
    home: Path,
    session: Optional[str] = None,
) -> dict:
    cfg = json.loads(cfg_path.read_text())
    agents = cfg.setdefault("agents", {})
    row: dict[str, Any] = dict(agents.get(name) or {})
    row["home"] = str(home)
    row["family"] = family
    row["yolo"] = True
    row["observer_enabled"] = True
    row["interjection_enabled"] = True
    if session:
        sid = str(session).strip()
        if not sid:
            raise ValueError("empty session id")
        row["session"] = sid
        row["short_id"] = sid[-4:]
    elif "session" not in row:
        # Do not mint. Leave session absent so restart_agent.sh will not invent one silently
        # if we refuse to launch without a session.
        pass
    agents[name] = row
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    return row


def attach_session(home: Path, session_id: str, *, grok_sessions: Optional[Path] = None) -> Path:
    """Move grok session files to this home's encoded cwd. Same UUID."""
    sid = session_id.strip()
    dest = grok_session_dir(home, sid)
    if grok_sessions is not None:
        encoded = str(home.resolve()).replace("/", "%2F")
        dest = Path(grok_sessions) / encoded / sid
    if dest.is_dir():
        return dest
    src = find_session_dir(sid, grok_sessions=grok_sessions)
    if src is None:
        raise FileNotFoundError(f"grok session {sid} not found under ~/.grok/sessions")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    summary = dest / "summary.json"
    if summary.exists():
        try:
            data = json.loads(summary.read_text())
            info = data.setdefault("info", {})
            info["cwd"] = str(home)
            info["id"] = sid
            summary.write_text(json.dumps(data, indent=2) + "\n")
        except Exception:
            pass
    return dest


def start_agent(
    family: str,
    name: str,
    *,
    session: Optional[str] = None,
    agents_json: Optional[Path] = None,
    launch: bool = False,
    restart_script: Optional[Path] = None,
    grok_sessions: Optional[Path] = None,
) -> dict:
    """Create home + roster. Attach existing session if given. Never mint."""
    cfg_path = Path(agents_json or Path.home() / "agents" / "config" / "agents.json")
    cfg = json.loads(cfg_path.read_text())
    agents_dir = _agents_dir(cfg)
    home = ensure_home(family, name, agents_dir=agents_dir)
    sid = (session or "").strip() or None
    if sid:
        attach_session(home, sid, grok_sessions=grok_sessions)
    row = upsert_roster(cfg_path, name=name, family=family, home=home, session=sid)
    launched = False
    if launch:
        if not sid:
            raise ValueError("refusing to launch without an existing session id (will not mint)")
        script = restart_script or (
            Path(__file__).resolve().parent.parent / "scripts" / "restart_agent.sh"
        )
        subprocess.run(["bash", str(script), name], check=True)
        launched = True
    return {
        "name": name,
        "family": family,
        "home": str(home),
        "session": sid,
        "roster": row,
        "launched": launched,
        "history_config": str(home / "asdaaas" / "history" / "config.json"),
    }
