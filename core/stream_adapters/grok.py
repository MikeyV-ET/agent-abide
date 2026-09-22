"""Grok → aa.stream adapter (native updates.jsonl → hot events)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aa_stream import (
    BODY_MAP_V,
    FORMAT_FAMILY,
    FORMAT_V,
    NATIVE_GROK,
    append_hot_events,
    build_event,
    default_hot_meta,
    ensure_aa_stream_layout,
    find_live_updates,
    hot_path,
    iso_utc,
    read_checkpoint,
    read_hot_meta,
    resolve_history_dir,
    write_checkpoint,
    write_hot_meta,
    _parse_ts,
)


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

    if kind == "retry_state":
        return "status", "full", "none", {
            "kind": "retry_state",
            "type": update.get("type") or "retrying",
            "attempt": update.get("attempt"),
            "max_retries": update.get("max_retries") or update.get("maxRetries"),
            "reason": update.get("reason") or "",
            "error_type": update.get("error_type") or update.get("errorType"),
        }

    if kind == "doom_loop_detected":
        return "status", "full", "none", {
            "kind": "doom_loop",
            "reason": update.get("reason") or update.get("message") or "doom_loop",
        }

    # unknown / other sessionUpdate
    if kind:
        return "unknown", "none", None, {"kind": "raw_only"}
    # non-session/update lines
    return "unknown", "none", None, {"kind": "raw_only"}


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
