"""
aa.stream v1 — AA-owned hot tip envelope + grok tailer.

Spec: docs/specs/aa_stream/HOT_FORMAT_v1.md
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from full_stream import (
    HOT_KEEP_BYTES,
    HOT_MAX_BYTES,
    agent_full_stream_dir,
    agent_history_dir,
    resolve_history_dir,
    ensure_layout,
    find_live_updates,
    iso_utc,
    _parse_ts,
)

FORMAT_FAMILY = "aa.stream"
FORMAT_V = 1
BODY_MAP_V = 1
HOT_NAME = "hot.jsonl"
HOT_META_NAME = "hot.meta.json"
SOURCES_DIR = "sources"
NATIVE_GROK = "grok.session_update.v1"


def hot_path(fs_dir: Path) -> Path:
    return Path(fs_dir) / HOT_NAME


def hot_meta_path(fs_dir: Path) -> Path:
    return Path(fs_dir) / HOT_META_NAME


def source_checkpoint_path(fs_dir: Path, backend: str) -> Path:
    return Path(fs_dir) / SOURCES_DIR / f"{backend}.json"


def ensure_aa_stream_layout(fs_dir: Path, agent: str) -> Path:
    """Create history layout including hot tip + meta. Returns fs_dir."""
    fs_dir = Path(fs_dir)
    ensure_layout(fs_dir)
    (fs_dir / SOURCES_DIR).mkdir(parents=True, exist_ok=True)
    hp = hot_path(fs_dir)
    if not hp.exists():
        hp.write_text("")
    mp = hot_meta_path(fs_dir)
    if not mp.exists():
        write_hot_meta(fs_dir, default_hot_meta(agent, fs_dir))
    # human pointer
    fmt = fs_dir / "FORMAT"
    if not fmt.exists():
        fmt.write_text(f"{FORMAT_FAMILY}/{FORMAT_V}\n")
    return fs_dir


def default_hot_meta(agent: str, fs_dir: Path) -> Dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "format": FORMAT_FAMILY,
        "v": FORMAT_V,
        "v_min_present": FORMAT_V,
        "v_max_present": FORMAT_V,
        "agent": agent,
        "created_at": now,
        "updated_at": now,
        "hot_path": HOT_NAME,
        "policy": {
            "max_bytes": HOT_MAX_BYTES,
            "keep_bytes": HOT_KEEP_BYTES,
        },
        "stream_seq_next": 0,
        "backends": {},
        "notes": "SA-first; AA-TUI not cut over; never prune backend-native files",
    }


def read_hot_meta(fs_dir: Path) -> Dict[str, Any]:
    mp = hot_meta_path(fs_dir)
    if not mp.exists():
        return {}
    return json.loads(mp.read_text(encoding="utf-8"))


def write_hot_meta(fs_dir: Path, meta: Dict[str, Any]) -> None:
    fs_dir = Path(fs_dir)
    mp = hot_meta_path(fs_dir)
    meta = dict(meta)
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    meta["format"] = FORMAT_FAMILY
    meta.setdefault("v", FORMAT_V)
    text = json.dumps(meta, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(fs_dir), suffix=".meta.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, mp)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_checkpoint(fs_dir: Path, backend: str) -> Dict[str, Any]:
    p = source_checkpoint_path(fs_dir, backend)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def write_checkpoint(fs_dir: Path, backend: str, data: Dict[str, Any]) -> None:
    fs_dir = Path(fs_dir)
    (fs_dir / SOURCES_DIR).mkdir(parents=True, exist_ok=True)
    p = source_checkpoint_path(fs_dir, backend)
    data = dict(data)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    fd, tmp = tempfile.mkstemp(dir=str(fs_dir / SOURCES_DIR), suffix=".ckpt.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def is_backend_native_path(path: Path) -> bool:
    """True if path looks like a backend-owned session file (do not prune)."""
    s = str(Path(path).resolve())
    markers = (
        "/.grok/sessions/",
        "/.codex/sessions/",
        "/.claude/projects/",
        "/.claude/sessions/",
    )
    return any(m in s for m in markers)


def assert_aa_hot_path(path: Path, fs_dir: Optional[Path] = None) -> Path:
    """Refuse prune/rewrite targets that are backend-native."""
    path = Path(path).resolve()
    if is_backend_native_path(path):
        raise ValueError(
            f"refusing to modify backend-native path (AA hot only): {path}"
        )
    if fs_dir is not None:
        expected = hot_path(fs_dir).resolve()
        if path != expected:
            parts = set(path.parts)
            aa_dir = bool(parts & {"history", "full_stream"})
            if path.name != HOT_NAME or not aa_dir:
                raise ValueError(
                    f"prune/seal apply target must be AA history/hot.jsonl, got: {path}"
                )
    return path




def build_event(
    *,
    agent: str,
    backend: str,
    session_id: str,
    stream_seq: int,
    native_schema: str,
    native_event: dict,
    ts: Optional[float] = None,
    class_: str = "unknown",
    phase: str = "none",
    role: Optional[str] = None,
    body: Optional[Dict[str, Any]] = None,
    source: Optional[Dict[str, Any]] = None,
    ids: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if ts is None:
        ts = _parse_ts(native_event) or datetime.now(timezone.utc).timestamp()
    ev: Dict[str, Any] = {
        "v": FORMAT_V,
        "format": FORMAT_FAMILY,
        "ts": ts,
        "ts_iso": iso_utc(ts),
        "agent": agent,
        "backend": backend,
        "session_id": session_id or "unknown",
        "stream_seq": stream_seq,
        "class": class_,
        "phase": phase,
        "native": {
            "schema": native_schema,
            "event": native_event,
        },
        "body_map_v": BODY_MAP_V,
        "ts_source": "backend" if _parse_ts(native_event) is not None else "ingest",
    }
    if role is not None:
        ev["role"] = role
    if body is not None:
        ev["body"] = body
    if source is not None:
        ev["source"] = source
    if ids is not None:
        ev["ids"] = ids
    return ev




# Backend adapters: stream_adapters.grok / .claude → append_hot_events
def tail_grok_once(*a, **k):
    from stream_adapters.grok import tail_grok_once as _t
    return _t(*a, **k)

def wrap_grok_line(*a, **k):
    from stream_adapters.grok import wrap_grok_line as _t
    return _t(*a, **k)

def map_grok_event(*a, **k):
    from stream_adapters.grok import map_grok_event as _t
    return _t(*a, **k)


def append_hot_events(fs_dir: Path, events: List[Dict[str, Any]]) -> int:
    """Append events to hot.jsonl. Returns bytes written."""
    if not events:
        return 0
    hp = hot_path(fs_dir)
    raw = "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in events)
    with open(hp, "a", encoding="utf-8") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    return len(raw.encode("utf-8"))




def iter_hot_events(fs_dir: Path) -> Iterator[Dict[str, Any]]:
    hp = hot_path(fs_dir)
    if not hp.exists():
        return
    with open(hp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue
