"""Claude → aa.stream adapter (native session jsonl → hot events).

Native path (Claude Code / SDK):
  ~/.claude/projects/<dash-encoded-cwd>/<session-id>.jsonl

Line types of interest: user, assistant (message.content blocks).
Other types (attachment, queue-operation, …) map to meta/skip.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aa_stream import (
    FORMAT_V,
    append_hot_events,
    build_event,
    default_hot_meta,
    ensure_aa_stream_layout,
    hot_path,
    read_checkpoint,
    read_hot_meta,
    resolve_history_dir,
    write_checkpoint,
    write_hot_meta,
)

NATIVE_CLAUDE = "claude.session_jsonl.v1"

def _recent_injected_texts(history_dir, limit: int = 40) -> set:
    """Texts recorded by ClaudeBackend._record_stdin_interjection (dedupe session echo)."""
    from pathlib import Path as _P
    side = _P(history_dir) / "injected_stdin.jsonl"
    if not side.is_file():
        return set()
    out = set()
    try:
        lines = side.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        for ln in lines:
            if not ln.strip():
                continue
            try:
                o = json.loads(ln)
                tx = o.get("text")
                if isinstance(tx, str) and tx.strip():
                    out.add(tx.strip())
            except Exception:
                continue
    except Exception:
        return set()
    return out



def _dash_encode_cwd(home: str | Path) -> str:
    s = str(home)
    # Claude projects use leading dash + path with / → -
    enc = s.replace("/", "-")
    if not enc.startswith("-"):
        enc = "-" + enc
    return enc


def find_live_session(
    agent_home: Path, session_id: Optional[str] = None
) -> Optional[Path]:
    """Locate Claude session transcript for this agent home."""
    agent_home = Path(agent_home).resolve()
    sid = session_id or ""

    # health.json session_id
    if not sid:
        health = agent_home / "asdaaas" / "health.json"
        if health.is_file():
            try:
                sid = json.loads(health.read_text(encoding="utf-8")).get("session_id") or ""
            except Exception:
                sid = ""

    # agents.json via session_locator when available
    if not sid:
        try:
            import sys

            api = Path(__file__).resolve().parents[2] / "api"
            if str(api) not in sys.path:
                sys.path.insert(0, str(api.parent))
            from api.session_locator import SessionLocator

            loc = SessionLocator()
            # match by home path
            for name, cfg in (loc._agents or {}).items():
                if Path(cfg.get("home") or "").resolve() == agent_home:
                    p = loc.session_file(name)
                    if p and p.exists():
                        return p.resolve()
                    sid = cfg.get("session") or sid
                    break
        except Exception:
            pass

    projects = Path.home() / ".claude" / "projects"
    enc = _dash_encode_cwd(agent_home)
    proj = projects / enc

    if sid:
        cand = proj / f"{sid}.jsonl"
        if cand.is_file():
            return cand.resolve()
        # session id sometimes without full uuid
        if proj.is_dir():
            for p in proj.glob(f"{sid}*.jsonl"):
                if p.is_file():
                    return p.resolve()

    if not proj.is_dir():
        # fuzzy: any project dir containing agent home name
        name = agent_home.name
        if projects.is_dir():
            matches = []
            for d in projects.iterdir():
                if not d.is_dir():
                    continue
                if name in d.name or enc in d.name:
                    for p in d.glob("*.jsonl"):
                        if p.is_file():
                            matches.append(p)
            if matches:
                return max(matches, key=lambda p: p.stat().st_mtime).resolve()
        return None

    # largest / newest jsonl in project dir
    files = [p for p in proj.glob("*.jsonl") if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime).resolve()


#: Base64 image payloads are the one thing in a Claude transcript that is
#: megabytes wide and worth nothing downstream. Unfiltered they land in
#: hot.jsonl twice — verbatim in `native`, truncated in `body` — and the TUI
#: paints them as a wall of characters. Keep the fact, drop the bytes.
ELIDE_OVER_CHARS = 4096


def _elide_blob(value: str, label: str = "data") -> str:
    return f"[{label}: {len(value)} chars elided]"


def _elide_binary(node):
    """Recursively replace oversized base64 payloads with a short marker.

    Structure is preserved so anything reading the shape of the event still
    works; only the payload string is replaced.
    """
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "data" and isinstance(v, str) and len(v) > ELIDE_OVER_CHARS:
                media = node.get("media_type") or "binary"
                out[k] = _elide_blob(v, media)
            else:
                out[k] = _elide_binary(v)
        return out
    if isinstance(node, list):
        return [_elide_binary(v) for v in node]
    if isinstance(node, str) and len(node) > ELIDE_OVER_CHARS * 8:
        return _elide_blob(node)
    return node


def _flatten_tool_result_content(content) -> str:
    """tool_result content may be a string or a list of blocks (text/image)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(_elide_binary(content), ensure_ascii=False)
    parts = []
    for b in content:
        if not isinstance(b, dict):
            parts.append(str(b))
            continue
        if b.get("type") == "image":
            src = b.get("source") or {}
            media = src.get("media_type") or "image"
            size = len(src.get("data") or "")
            parts.append(f"[image: {media}, {size} chars elided]")
        elif b.get("type") == "text":
            parts.append(b.get("text") or "")
        else:
            parts.append(json.dumps(_elide_binary(b), ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def _content_blocks(obj: dict) -> list:
    msg = obj.get("message") or {}
    c = msg.get("content")
    if isinstance(c, list):
        return c
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    if c is None and isinstance(obj.get("content"), (str, list)):
        c = obj["content"]
        if isinstance(c, list):
            return c
        return [{"type": "text", "text": str(c)}]
    return []


def _text_from_blocks(blocks: list) -> str:
    parts = []
    for b in blocks:
        if not isinstance(b, dict):
            if isinstance(b, str):
                parts.append(b)
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text") or "")
        elif t == "thinking":
            # thinking handled separately
            continue
        elif t == "tool_use":
            parts.append(f"[tool: {b.get('name', '?')}]")
        elif t == "tool_result":
            content = _flatten_tool_result_content(b.get("content", ""))
            parts.append(f"[result: {content[:200]}]")
    return "\n".join(p for p in parts if p)


def map_claude_event(obj: dict) -> Tuple[str, str, Optional[str], Optional[Dict[str, Any]]]:
    """
    Returns (class, phase, role|None, body|None).
    One native line may conceptually hold multiple blocks; we emit a single
    primary body (text preferred; else first tool_use; else raw).
    """
    msg_type = obj.get("type") or ""

    if msg_type == "user":
        blocks = _content_blocks(obj)
        # tool_result-only user lines
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        texts = [b for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        if results and not texts:
            b0 = results[0]
            content = _flatten_tool_result_content(b0.get("content", ""))
            if len(content) > 200_000:
                content = content[:200_000]
            return "tool", "end", "tool", {
                "kind": "tool_result",
                "tool_id": str(b0.get("tool_use_id") or b0.get("tool_useId") or "") or None,
                "content": content,
                "status": "completed",
            }
        text = _text_from_blocks(blocks)
        if not text and isinstance((obj.get("message") or {}).get("content"), str):
            text = (obj.get("message") or {}).get("content") or ""
        body = {"kind": "text", "text": text} if text else {"kind": "raw_only"}
        return "message", "full", "user", body

    if msg_type == "assistant":
        blocks = _content_blocks(obj)
        thinking_parts = []
        text_parts = []
        tools = []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "thinking":
                th = b.get("thinking") or b.get("text") or ""
                if th:
                    thinking_parts.append(th)
            elif t == "text":
                text_parts.append(b.get("text") or "")
            elif t == "tool_use":
                tools.append(b)
        if text_parts:
            text = "\n".join(text_parts)
            return "message", "full", "assistant", {"kind": "text", "text": text}
        if thinking_parts:
            text = "\n".join(thinking_parts)
            return "thought", "full", "assistant", {"kind": "thinking", "text": text}
        if tools:
            b = tools[0]
            return "tool", "start", "assistant", {
                "kind": "tool_call",
                "id": str(b.get("id") or "") or None,
                "name": str(b.get("name") or "tool"),
                "args": b.get("input") or b.get("arguments"),
                "status": "started",
            }
        return "message", "full", "assistant", {"kind": "raw_only"}

    if msg_type in ("system", "progress", "error"):
        return "meta", "none", "none", {"kind": "meta", "label": msg_type}

    if msg_type:
        # attachment, queue-operation, atis-latch, last-prompt, …
        return "meta", "none", "none", {"kind": "meta", "label": msg_type or "meta"}

    return "unknown", "none", None, {"kind": "raw_only"}


def wrap_claude_line(
    obj: dict,
    *,
    agent: str,
    session_id: str,
    stream_seq: int,
    source_path: str,
    offset: int,
) -> Dict[str, Any]:
    class_, phase, role, body = map_claude_event(obj)
    return build_event(
        agent=agent,
        backend="claude",
        session_id=session_id,
        stream_seq=stream_seq,
        native_schema=NATIVE_CLAUDE,
        native_event=_elide_binary(obj),
        class_=class_,
        phase=phase,
        role=role,
        body=body,
        source={"path": source_path, "offset": offset},
    )


def tail_claude_once(
    agent_home: Path,
    agent: str,
    *,
    session_id: Optional[str] = None,
    source: Optional[Path] = None,
    max_lines: Optional[int] = None,
    max_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Read new bytes from Claude session jsonl since checkpoint; append aa.stream to hot.
    """
    agent_home = Path(agent_home)
    fs_dir = ensure_aa_stream_layout(resolve_history_dir(agent_home), agent)
    src = Path(source) if source else find_live_session(agent_home, session_id)
    if not src or not src.exists():
        return {"status": "error", "error": "claude session jsonl not found"}

    src = src.resolve()
    sid = session_id or src.stem
    ckpt = read_checkpoint(fs_dir, "claude")
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
            if not line.endswith(b"\n"):
                break
            new_offset = f.tell()
            bytes_read += len(line)
            lines += 1
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            # skip pure chrome that has no display value? still wrap as meta for completeness
            ev = wrap_claude_line(
                obj,
                agent=agent,
                session_id=sid,
                stream_seq=seq,
                source_path=str(src),
                offset=line_off,
            )
            # Skip session echo of stdin-injected text (already hot as interjection)
            body = ev.get("body") or {}
            if (
                ev.get("role") == "user"
                and body.get("kind") == "text"
                and isinstance(body.get("text"), str)
            ):
                inj = _recent_injected_texts(fs_dir)
                if body["text"].strip() in inj:
                    continue
            events.append(ev)
            seq += 1

    written = append_hot_events(fs_dir, events)

    write_checkpoint(
        fs_dir,
        "claude",
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
    meta["backends"]["claude"] = {
        "session_id": sid,
        "source_path": str(src),
        "byte_offset": new_offset,
        "last_ts": events[-1]["ts"] if events else meta.get("backends", {}).get("claude", {}).get("last_ts"),
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
