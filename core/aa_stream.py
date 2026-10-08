"""
aa.stream v1 — AA-owned hot tip envelope + grok tailer.

Spec: docs/specs/aa_stream/HOT_FORMAT_v1.md
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
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

_prune_lock = threading.Lock()


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


def tail_claude_once(*a, **k):
    from stream_adapters.claude import tail_claude_once as _t
    return _t(*a, **k)

def wrap_claude_line(*a, **k):
    from stream_adapters.claude import wrap_claude_line as _t
    return _t(*a, **k)

def map_claude_event(*a, **k):
    from stream_adapters.claude import map_claude_event as _t
    return _t(*a, **k)



def maybe_prune_hot(
    fs_dir: Path,
    *,
    agent: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """If hot.jsonl is at/over policy max_bytes: seal prefix to V2, keep tail.

    Never touches backend-native logs. On seal/verify failure, hot is left
    intact (prune_hot refuses to rewrite). Concurrent callers share one lock.
    """
    fs_dir = Path(fs_dir)
    hp = hot_path(fs_dir)
    if not hp.exists():
        return None
    meta = read_hot_meta(fs_dir) or {}
    policy = meta.get("policy") or {}
    try:
        max_b = int(policy.get("max_bytes") or HOT_MAX_BYTES)
        keep_b = int(policy.get("keep_bytes") or HOT_KEEP_BYTES)
    except (TypeError, ValueError):
        max_b, keep_b = HOT_MAX_BYTES, HOT_KEEP_BYTES
    try:
        size = hp.stat().st_size
    except OSError:
        return None
    if size < max_b:
        return None
    if not _prune_lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        size = hp.stat().st_size
        if size < max_b:
            return None
        agent = agent or meta.get("agent")
        grok = (meta.get("backends") or {}).get("grok") or {}
        session_id = session_id or grok.get("session_id")
        from full_stream import prune_hot

        result = prune_hot(
            hp,
            fs_dir,
            apply=True,
            agent=agent,
            session_id=session_id,
            max_bytes=max_b,
            keep_bytes=keep_b,
        )
        after = hp.stat().st_size
        print(
            f"[hot] prune status={result.get('status')} "
            f"before={size} after={after} agent={agent}"
        )
        return result
    except Exception as e:
        print(f"[hot] WARN prune failed (hot left intact): {e}")
        return {"status": "error", "error": str(e)}
    finally:
        _prune_lock.release()


def _event_key(o: Dict[str, Any]) -> Optional[Tuple[float, str]]:
    """Unique-ish tape key: (ts, session_id). ts primary; uuid breaks a same-second tie."""
    ts = o.get("ts")
    if ts is None:
        return None
    try:
        ts_f = float(ts)
    except (TypeError, ValueError):
        return None
    sid = o.get("session_id") or ""
    return (ts_f, sid)


def last_hot_key(fs_dir: Path) -> Optional[Tuple[float, str]]:
    hp = hot_path(fs_dir)
    if not hp.exists() or hp.stat().st_size == 0:
        return None
    try:
        with open(hp, "rb") as f:
            f.seek(0, 2)
            n = f.tell()
            f.seek(max(0, n - 1_000_000))
            if n > 1_000_000:
                f.readline()
            lines = f.readlines()
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            o = json.loads(raw)
            k = _event_key(o)
            if k is not None:
                return k
    except Exception:
        return None
    return None


def last_hot_ts(fs_dir: Path) -> Optional[float]:
    k = last_hot_key(fs_dir)
    return None if k is None else k[0]


def check_hot_ts_monotonic(fs_dir: Path) -> Dict[str, Any]:
    """Scan hot.jsonl: (ts, session_id) must be non-decreasing.

    ts is primary. Same ts: session_id breaks the tie. Equal keys are legal
    (two grok lines in the same second, same session). Backward is error.
    """
    fs_dir = Path(fs_dir)
    hp = hot_path(fs_dir)
    out: Dict[str, Any] = {
        "status": "ok",
        "path": str(hp),
        "lines": 0,
        "inversions": [],
    }
    if not hp.exists():
        out["status"] = "missing"
        _write_continuity(fs_dir, out)
        return out
    prev: Optional[Tuple[float, str]] = None
    prev_seq = None
    n = 0
    with open(hp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            k = _event_key(o)
            if k is None:
                continue
            n += 1
            if prev is not None and k < prev:
                inv = {
                    "line": n,
                    "ts": k[0],
                    "ts_iso": o.get("ts_iso"),
                    "session_id": k[1],
                    "prev_ts": prev[0],
                    "prev_session_id": prev[1],
                    "stream_seq": o.get("stream_seq"),
                    "prev_stream_seq": prev_seq,
                }
                out["inversions"].append(inv)
                if len(out["inversions"]) >= 20:
                    break
            prev = k
            prev_seq = o.get("stream_seq")
    out["lines"] = n
    if out["inversions"]:
        out["status"] = "error"
        print(
            f"[hot] ERROR (ts, session_id) not monotonic: {len(out['inversions'])} "
            f"inversion(s) in {hp} e.g. seq={out['inversions'][0].get('stream_seq')}"
        )
    _write_continuity(fs_dir, out)
    return out


def check_history_continuity(fs_dir: Path) -> Dict[str, Any]:
    """Hot monotonic ts, plus archive chunks whose last-line ts < first-line ts.

    Interior chunk inversions need a decompress scan (CLI check-continuity).
    This cheap pass flags the inverted from_ts/until_ts we already have.
    """
    fs_dir = Path(fs_dir)
    hot = check_hot_ts_monotonic(fs_dir)
    chunk_inv: List[Dict[str, Any]] = []
    try:
        from full_stream import read_manifest

        for rec in read_manifest(fs_dir):
            a, b = rec.from_ts, rec.until_ts
            if a is not None and b is not None and b < a:
                chunk_inv.append(
                    {
                        "chunk_id": rec.chunk_id,
                        "from_ts": a,
                        "until_ts": b,
                        "from_ts_iso": rec.from_ts_iso,
                        "until_ts_iso": rec.until_ts_iso,
                        "plain_bytes": rec.plain_bytes,
                    }
                )
    except Exception as e:
        chunk_inv.append({"error": str(e)})
    status = "ok"
    if hot.get("status") == "error" or chunk_inv:
        status = "error"
        if chunk_inv:
            print(
                f"[hot] ERROR archive chunk ts not sequential: "
                f"{[c.get('chunk_id') for c in chunk_inv]}"
            )
    out = {
        "status": status,
        "hot": hot,
        "chunk_inversions": chunk_inv,
    }
    _write_continuity(fs_dir, out)
    return out


def _write_continuity(fs_dir: Path, payload: Dict[str, Any]) -> None:
    payload = dict(payload)
    payload["checked_at"] = datetime.now(timezone.utc).isoformat()
    path = Path(fs_dir) / "continuity.json"
    try:
        path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    except Exception:
        pass


def append_hot_events(fs_dir: Path, events: List[Dict[str, Any]]) -> int:
    """Append events to hot.jsonl. Returns bytes written.

    Refuses events whose (ts, session_id) is behind the hot tip.
    """
    if not events:
        return 0
    fs_dir = Path(fs_dir)
    tip = last_hot_key(fs_dir)
    if tip is not None:
        kept: List[Dict[str, Any]] = []
        dropped = 0
        for e in events:
            if not isinstance(e, dict):
                kept.append(e)
                continue
            k = _event_key(e)
            if k is not None and k < tip:
                dropped += 1
                continue
            kept.append(e)
            if k is not None:
                tip = k
        if dropped:
            print(
                f"[hot] ERROR refused {dropped} event(s) with (ts, session_id) "
                f"< hot tip; continuity broken — not appending backward"
            )
            _write_continuity(
                fs_dir,
                {
                    "status": "error",
                    "reason": "append_refused_backward_key",
                    "dropped": dropped,
                    "kept": len(kept),
                    "tip": list(tip) if tip else None,
                },
            )
        events = kept
        if not events:
            return 0
    hp = hot_path(fs_dir)
    raw = "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in events)
    with open(hp, "a", encoding="utf-8") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    sid = None
    last = events[-1] if events else None
    if isinstance(last, dict):
        sid = last.get("session_id")
    agent = None
    meta = read_hot_meta(fs_dir) or {}
    agent = meta.get("agent")
    maybe_prune_hot(fs_dir, agent=agent, session_id=sid)
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
