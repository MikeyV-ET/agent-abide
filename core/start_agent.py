"""Create a family agent home and roster row.

Citizen families never mint a session id. The testagent family may session/new.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

MINT_OK_FAMILIES = {"testagent", "test"}

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



def parse_start_tokens(arg: str) -> dict:
    """Parse `/start` tail: family name [session-uuid] [model]."""
    import re
    parts = (arg or "").split()
    if len(parts) < 2:
        raise ValueError("need family and name")
    family, name = parts[0], parts[1]
    session = None
    model = None
    uuid_re = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    for tok in parts[2:]:
        if uuid_re.match(tok):
            session = tok
        else:
            model = tok
    return {"family": family, "name": name, "session": session, "model": model}


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
    model: Optional[str] = None,
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
        # Citizen families: leave session absent (do not mint).
        pass
    if model:
        row["model"] = str(model).strip()
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
    model: Optional[str] = None,
) -> dict:
    """Create home + roster. Attach existing session if given.

    Minting (session/new) is allowed only for MINT_OK_FAMILIES.
    """
    cfg_path = Path(agents_json or Path.home() / "agents" / "config" / "agents.json")
    cfg = json.loads(cfg_path.read_text())
    agents_dir = _agents_dir(cfg)
    home = ensure_home(family, name, agents_dir=agents_dir)
    sid = (session or "").strip() or None
    if sid:
        attach_session(home, sid, grok_sessions=grok_sessions)
    row = upsert_roster(
        cfg_path, name=name, family=family, home=home, session=sid, model=model
    )
    launched = False
    if launch:
        mint_ok = family.strip().lower() in MINT_OK_FAMILIES
        if not sid and not mint_ok:
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

def grok_sessions_parent(home: Path) -> Path:
    encoded = str(Path(home).expanduser().resolve()).replace("/", "%2F")
    return Path.home() / ".grok" / "sessions" / encoded


def list_sessions_for_home(home: Path, *, skip: str | None = None) -> list[dict]:
    """UUIDs under this home's grok session dir."""
    parent = grok_sessions_parent(home)
    out = []
    if not parent.is_dir():
        return out
    skip = (skip or "").strip()
    for d in sorted(parent.iterdir()):
        if not d.is_dir():
            continue
        name = d.name
        if len(name) < 8:
            continue
        if skip and name == skip:
            kind = "spine"
        else:
            kind = "extra"
        live = session_is_live(name)
        out.append({"session": name, "kind": kind, "live": live, "path": str(d)})
    return out


def session_is_live(session_id: str) -> bool:
    needle = (session_id or "").strip()
    if len(needle) < 8:
        return False
    import os
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
        except Exception:
            continue
        if needle in cmd and ("grok" in cmd or "asdaaas" in cmd):
            return True
    return False


def ingest_session_history(home: Path, session_id: str, *, source_home: Path) -> dict:
    """Project parent-encoded updates.jsonl into this seat's hot.jsonl. Does not move files."""
    from stream_adapters.grok import tail_grok_once
    src = grok_sessions_parent(source_home) / session_id / "updates.jsonl"
    if not src.is_file():
        raise FileNotFoundError(f"no updates.jsonl for {session_id} under {source_home}")
    write_history_config(home)
    return tail_grok_once(home, Path(home).name, session_id=session_id, source=src)


def seat_subagent(
    parent: str,
    session: str,
    name: str | None = None,
    *,
    agents_json: Path | None = None,
    launch: bool = True,
) -> dict:
    """Temporary sub family seat. Does not move the grok session. Does not mint."""
    sid = (session or "").strip()
    if not sid:
        raise ValueError("session uuid required")
    cfg_path = Path(agents_json or Path.home() / "agents" / "config" / "agents.json")
    cfg = json.loads(cfg_path.read_text())
    agents = cfg.get("agents") or {}
    parent_row = agents.get(parent) or {}
    parent_home = Path(parent_row.get("home") or (Path.home() / "agents" / parent))
    short = sid.replace("-", "")[-6:]
    seat_name = (name or f"{parent}-{short}").strip()
    agents_dir = _agents_dir(cfg)
    home = ensure_home("sub", seat_name, agents_dir=agents_dir)
    ingest = ingest_session_history(home, sid, source_home=parent_home)
    row = upsert_roster(
        cfg_path, name=seat_name, family="sub", home=home, session=sid
    )
    live = session_is_live(sid)
    launched = False
    if launch and not live:
        script = Path(__file__).resolve().parent.parent / "scripts" / "restart_agent.sh"
        subprocess.run(["bash", str(script), seat_name], check=False)
        launched = True
    return {
        "name": seat_name,
        "family": "sub",
        "home": str(home),
        "session": sid,
        "parent": parent,
        "live_child": live,
        "launched": launched,
        "ingest": ingest,
        "roster": row,
    }

