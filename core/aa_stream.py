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


# ----- grok → aa.stream mapping -----

def _grok_session_update_kind(obj: dict) -> str:
    try:
        return (
            obj.get("params", {})
            .get("update", {})
            .get("sessionUpdate")
            or obj.get("params", {})
            .get("update", {})
            .get("session_update")
            or ""
        )
    except Exception:
        return ""


def _extract_text_chunks(update: dict) -> str:
    """Best-effort text from grok update payloads."""
    # shapes: str | {type,text}| {text} | [{type,text}, ...] | nested content
    def from_val(v) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            if isinstance(v.get("text"), str):
                return v["text"]
            if isinstance(v.get("content"), (str, list, dict)):
                return from_val(v.get("content"))
            if isinstance(v.get("thought"), str):
                return v["thought"]
            return ""
        if isinstance(v, list):
            return "".join(from_val(item) for item in v)
        return ""

    for key in ("content", "text", "thought", "message"):
        s = from_val(update.get(key))
        if s:
            return s
    return ""


def map_grok_event(obj: dict) -> Tuple[str, str, Optional[str], Optional[Dict[str, Any]]]:
    """
    Returns (class, phase, role|None, body|None).
    body may be None → caller may set raw_only or omit.
    """
    kind = _grok_session_update_kind(obj)
    update = {}
    try:
        update = obj.get("params", {}).get("update", {}) or {}
    except Exception:
        pass
    text = _extract_text_chunks(update)

    if kind in ("user_message_chunk", "user_message"):
        body = {"kind": "text_delta" if "chunk" in kind else "text", "text": text} if text else {"kind": "raw_only"}
        return "message", "delta" if "chunk" in kind else "full", "user", body

    if kind in ("agent_message_chunk", "agent_message", "message_chunk"):
        body = {"kind": "text_delta" if "chunk" in kind else "text", "text": text} if text else {"kind": "raw_only"}
        return "message", "delta" if "chunk" in kind else "full", "assistant", body

    if kind in ("agent_thought_chunk", "agent_thought", "thought_chunk"):
        body = {"kind": "thinking_delta" if "chunk" in kind else "thinking", "text": text} if text else {"kind": "raw_only"}
        return "thought", "delta" if "chunk" in kind else "full", "assistant", body

    if kind in ("tool_call", "tool_call_start"):
        tool_id = update.get("toolCallId") or update.get("tool_call_id") or update.get("id")
        name = update.get("title") or update.get("name") or update.get("kind") or "tool"
        return "tool", "start", "assistant", {
            "kind": "tool_call",
            "id": str(tool_id) if tool_id is not None else None,
            "name": str(name),
            "args": update.get("rawInput") or update.get("input") or update.get("arguments"),
            "status": "started",
        }

    if kind in ("tool_call_update", "tool_call_progress"):
        tool_id = update.get("toolCallId") or update.get("tool_call_id") or update.get("id")
        status = update.get("status") or "update"
        # completed tool often carries output
        content = update.get("rawOutput") or update.get("output") or update.get("content")
        if content is not None and status in ("completed", "failed", "done", "error"):
            cstr = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            return "tool", "end", "tool", {
                "kind": "tool_result",
                "tool_id": str(tool_id) if tool_id is not None else None,
                "content": cstr[:200_000] if len(cstr) > 200_000 else cstr,
                "status": str(status),
            }
        return "tool", "delta", "assistant", {
            "kind": "tool_call",
            "id": str(tool_id) if tool_id is not None else None,
            "name": str(update.get("title") or update.get("name") or "tool"),
            "status": str(status),
        }

    if kind in ("turn_completed", "turn_started", "turn_complete"):
        return "meta", "end" if "completed" in kind or "complete" in kind else "start", "none", {
            "kind": "meta",
            "label": kind,
        }

    if "compaction" in kind or kind in ("session_info", "available_commands_update", "current_mode_update"):
        return "meta", "none", "none", {"kind": "meta", "label": kind or "meta"}

    if kind in ("usage_update", "token_usage"):
        return "usage", "full", "none", {
            "kind": "usage",
            "input": update.get("inputTokens") or update.get("input"),
            "output": update.get("outputTokens") or update.get("output"),
            "total": update.get("totalTokens") or update.get("total"),
        }

    # unknown / other sessionUpdate
    if kind:
        return "unknown", "none", None, {"kind": "raw_only"}
    # non-session/update lines
    return "unknown", "none", None, {"kind": "raw_only"}


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


def wrap_grok_line(
    obj: dict,
    *,
    agent: str,
    session_id: str,
    stream_seq: int,
    source_path: str,
    offset: int,
) -> Dict[str, Any]:
    class_, phase, role, body = map_grok_event(obj)
    return build_event(
        agent=agent,
        backend="grok",
        session_id=session_id,
        stream_seq=stream_seq,
        native_schema=NATIVE_GROK,
        native_event=obj,
        class_=class_,
        phase=phase,
        role=role,
        body=body,
        source={"path": source_path, "offset": offset},
    )


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


def tail_grok_once(
    agent_home: Path,
    agent: str,
    *,
    session_id: Optional[str] = None,
    source: Optional[Path] = None,
    max_lines: Optional[int] = None,
    max_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Read new bytes from grok updates.jsonl since checkpoint; append aa.stream lines to hot.
    Returns stats dict.
    """
    agent_home = Path(agent_home)
    fs_dir = ensure_aa_stream_layout(resolve_history_dir(agent_home), agent)
    src = Path(source) if source else find_live_updates(agent_home, session_id)
    if not src or not src.exists():
        return {"status": "error", "error": "grok updates.jsonl not found"}

    src = src.resolve()
    sid = session_id or src.parent.name
    ckpt = read_checkpoint(fs_dir, "grok")
    # reset offset if path changed or file truncated
    offset = int(ckpt.get("byte_offset") or 0)
    if ckpt.get("path") and Path(ckpt["path"]).resolve() != src:
        offset = 0
    size = src.stat().st_size
    if offset > size:
        offset = 0

    meta = read_hot_meta(fs_dir) or default_hot_meta(agent, fs_dir)
    seq = int(meta.get("stream_seq_next") or 0)

    events: List[Dict[str, Any]] = []
    bytes_read = 0
    lines = 0
    new_offset = offset

    with open(src, "rb") as f:
        f.seek(offset)
        while True:
            if max_lines is not None and lines >= max_lines:
                break
            if max_bytes is not None and bytes_read >= max_bytes:
                break
            line_off = f.tell()
            line = f.readline()
            if not line:
                break
            # incomplete last line (no newline) — wait for next tail
            if not line.endswith(b"\n"):
                break
            new_offset = f.tell()
            bytes_read += len(line)
            lines += 1
            try:
                obj = json.loads(line)
            except Exception:
                # still advance; record as unknown wrapper with raw string? skip corrupt
                continue
            if not isinstance(obj, dict):
                continue
            ev = wrap_grok_line(
                obj,
                agent=agent,
                session_id=sid,
                stream_seq=seq,
                source_path=str(src),
                offset=line_off,
            )
            events.append(ev)
            seq += 1

    written = append_hot_events(fs_dir, events)

    # checkpoints + meta
    write_checkpoint(
        fs_dir,
        "grok",
        {
            "path": str(src),
            "byte_offset": new_offset,
            "session_id": sid,
            "last_ts": events[-1]["ts"] if events else ckpt.get("last_ts"),
            "lines_total_ingested": int(ckpt.get("lines_total_ingested") or 0) + len(events),
        },
    )
    meta["stream_seq_next"] = seq
    meta["agent"] = agent
    meta.setdefault("backends", {})
    meta["backends"]["grok"] = {
        "session_id": sid,
        "source_path": str(src),
        "byte_offset": new_offset,
        "last_ts": events[-1]["ts"] if events else meta.get("backends", {}).get("grok", {}).get("last_ts"),
    }
    if events:
        meta["v_min_present"] = FORMAT_V
        meta["v_max_present"] = FORMAT_V
    write_hot_meta(fs_dir, meta)

    return {
        "status": "ok",
        "source": str(src),
        "session_id": sid,
        "offset_before": offset,
        "offset_after": new_offset,
        "lines_ingested": len(events),
        "bytes_source_read": bytes_read,
        "bytes_hot_written": written,
        "stream_seq_next": seq,
        "hot": str(hot_path(fs_dir)),
        "hot_bytes": hot_path(fs_dir).stat().st_size,
    }


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
