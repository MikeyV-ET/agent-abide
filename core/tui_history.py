"""TUI history SoR: prefer asdaaas/history/hot.jsonl (aa.stream).

P0: path resolve + parse.
P1: live tail helpers — map aa.stream events → grok-shaped updates so
existing TUI _dispatch_event / ChatState reducers keep working.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator, Optional

from aa_stream_parser import is_aa_stream_path, parse_aa_stream

# History paint must stay responsive; pre-elision hot lines can be >1MB.
MAX_TUI_LINE_BYTES = 32 * 1024
MAX_NATIVE_PASSTHROUGH_BYTES = 16 * 1024


def agent_history_dir(agent_home: Path) -> Path:
    return Path(agent_home) / "asdaaas" / "history"


def hot_jsonl_path(agent_home: Path) -> Path:
    return agent_history_dir(agent_home) / "hot.jsonl"


def updates_jsonl_candidates(agent_home: Path) -> list[Path]:
    """Legacy grok session updates paths (best-effort)."""
    home = Path(agent_home)
    cands = []
    for p in [
        home / "asdaaas" / "updates.jsonl",
        home / "updates.jsonl",
    ]:
        if p.exists():
            cands.append(p)
    return cands


def resolve_history_source(agent_home: Path, prefer: Optional[str] = None) -> tuple[str, Path]:
    """Return (kind, path) kind in hot|updates|none.

    prefer: env TUI_HISTORY_SOURCE or arg: hot|updates|auto
    """
    prefer = (prefer or os.environ.get("TUI_HISTORY_SOURCE") or "auto").lower()
    hot = hot_jsonl_path(agent_home)
    if prefer == "hot":
        return ("hot", hot) if hot.exists() else ("none", hot)
    if prefer == "updates":
        ups = updates_jsonl_candidates(agent_home)
        return ("updates", ups[0]) if ups else ("none", hot)
    # auto
    if hot.exists() and hot.stat().st_size > 0:
        return ("hot", hot)
    ups = updates_jsonl_candidates(agent_home)
    if ups:
        return ("updates", ups[0])
    return ("none", hot)


def entries_from_hot(path: Path) -> list[dict[str, Any]]:
    return list(parse_aa_stream(path))


def entry_to_tui_lines(entry: dict[str, Any]) -> list[str]:
    """Minimal paint lines for catch-up (P0)."""
    role = (entry.get("role") or entry.get("type") or "").lower()
    text = entry.get("content") or entry.get("text") or ""
    if isinstance(text, list):
        text = " ".join(
            (x.get("text") if isinstance(x, dict) else str(x)) for x in text
        )
    text = str(text).strip()
    if not text:
        return []
    if role in ("user", "human"):
        return [f"You: {text}"]
    if role in ("assistant", "agent"):
        return [text]
    if role in ("system", "control"):
        return [f"[{text}]"]
    return [text]


def _decode_tool_output(content) -> str:
    """Turn grok/bash tool payload into plain text (for interjection scan)."""
    import json as _json
    if content is None:
        return ""
    if isinstance(content, str):
        s = content
        # JSON-encoded bash wrapper?
        if s.startswith("{") and ("output" in s or "type" in s):
            try:
                content = _json.loads(s)
            except Exception:
                return s
        else:
            return s
    if isinstance(content, list):
        # content[] blocks already
        parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "content":
                inner = item.get("content") or {}
                if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                    parts.append(inner["text"])
                elif isinstance(inner, str):
                    parts.append(inner)
            elif item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    if isinstance(content, dict):
        # {'type':'Bash','output':[byte ints]} or {'text':...}
        if isinstance(content.get("text"), str):
            return content["text"]
        out = content.get("output")
        if isinstance(out, list) and out and isinstance(out[0], int):
            try:
                return bytes(out).decode("utf-8", errors="replace")
            except Exception:
                return ""
        if isinstance(out, str):
            return out
        if isinstance(out, list):
            # list of strings?
            return "".join(str(x) for x in out)
        return _json.dumps(content, ensure_ascii=False)
    return str(content)



def tui_content_text(update: dict[str, Any] | None) -> str:
    """Safe text extract from a grok-shaped update["content"] field."""
    if not isinstance(update, dict):
        return ""
    c = update.get("content")
    if isinstance(c, dict):
        t = c.get("text")
        return t if isinstance(t, str) else ("" if t is None else str(t))
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for item in c:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                else:
                    inner = item.get("content")
                    if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                        parts.append(inner["text"])
                    elif isinstance(inner, str):
                        parts.append(inner)
        return "".join(parts)
    return ""


def tui_update_dict(event: dict[str, Any] | None) -> dict[str, Any]:
    """event["params"]["update"] as a dict, or {}."""
    if not isinstance(event, dict):
        return {}
    params = event.get("params")
    if not isinstance(params, dict):
        return {}
    upd = params.get("update")
    return upd if isinstance(upd, dict) else {}

def aa_event_to_tui_update(ev: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Map one aa.stream v1 event → grok session/update shape for TUI dispatch.

    Grok-sourced hot lines keep the full native updates.jsonl event in
    ``native.event``. Pass that through so tool stdout / <interjection> blocks
    match prod TUI behavior. Body-based mapping is the fallback (Claude, etc.).
    """
    if not isinstance(ev, dict):
        return None
    if ev.get("format") and ev.get("format") != "aa.stream":
        return None

    native = (ev.get("native") or {}).get("event")
    backend = (ev.get("backend") or "").lower()
    body = ev.get("body") or {}
    body_kind = (body.get("kind") if isinstance(body, dict) else None) or ""

    # Body-first paint when body already has display content (avoids re-hydrating
    # multi-MB native base64 / tool blobs just to show a line of text).
    paint_kinds = {
        "text", "text_delta", "thinking", "thinking_delta",
        "interjection", "tool_call", "tool_result",
    }

    def _body_worth_painting(b: dict, kind: str) -> bool:
        """Lossy body {kind: tool_call} must not beat native with real content."""
        if kind in ("text", "text_delta", "thinking", "thinking_delta", "interjection"):
            return bool(b.get("text"))
        if kind == "tool_result":
            return bool(b.get("content") or b.get("text"))
        if kind == "tool_call":
            return bool(b.get("args") or b.get("name") or b.get("id") or b.get("tool_id"))
        return False

    if isinstance(body, dict) and body_kind in paint_kinds and _body_worth_painting(body, body_kind):
        # fall through to body mapping below (skip native pass-through)
        pass
    elif isinstance(native, dict):
        # Prefer native grok session/update only when small enough
        try:
            import json as _json
            native_size = len(_json.dumps(native, ensure_ascii=False))
        except Exception:
            native_size = MAX_NATIVE_PASSTHROUGH_BYTES + 1
        if native_size <= MAX_NATIVE_PASSTHROUGH_BYTES:
            upd = (native.get("params") or {}).get("update")
            if isinstance(upd, dict) and upd.get("sessionUpdate"):
                out = {
                    "timestamp": native.get("timestamp", ev.get("ts")),
                    "method": native.get("method", "session/update"),
                    "params": {"update": dict(upd)},
                    "_aa_stream": True,
                    "_aa_class": ev.get("class"),
                    "_aa_seq": ev.get("stream_seq"),
                    "_aa_backend": backend or "grok",
                }
                return out
        # else: fall through to body mapping

    kind = body.get("kind") or ""
    role = (ev.get("role") or "").lower()
    text = body.get("text") if isinstance(body.get("text"), str) else ""
    ts = ev.get("ts")

    def _frame(session_update: str, update: dict) -> dict:
        u = dict(update)
        u["sessionUpdate"] = session_update
        return {
            "timestamp": ts,
            "method": "session/update",
            "params": {"update": u},
            "_aa_stream": True,
            "_aa_class": ev.get("class"),
            "_aa_seq": ev.get("stream_seq"),
            "_aa_backend": backend,
        }

    if kind == "interjection":
        if not text:
            return None
        # Reuse TUI InterjectionBlock path (same as tool stdout extract)
        wrapped = f"<interjection>\n{text}\n</interjection>"
        tid = f"ij-{abs(hash(text)) % 10_000_000}"
        fr = _frame(
            "tool_call_update",
            {
                "toolCallId": tid,
                "status": "completed",
                "title": "interjection",
                "rawOutput": wrapped,
                "content": [
                    {
                        "type": "content",
                        "content": {"type": "text", "text": wrapped},
                    }
                ],
            },
        )
        fr["params"]["update"]["_aa_interjection"] = True
        return fr

    if kind in ("text_delta", "text"):
        if not text:
            return None
        # Cap paint size — history scroll must not mount multi-10k Rich docs
        if len(text) > 8000:
            text = text[:8000] + "\n… [truncated for TUI]"
        if role in ("user", "human"):
            return _frame("user_message_chunk", {"content": {"text": text}})
        return _frame("agent_message_chunk", {"content": {"text": text}})

    if kind in ("thinking_delta", "thinking"):
        if not text:
            return None
        return _frame("agent_thought_chunk", {"content": {"text": text}})

    if kind == "tool_call":
        tid = body.get("id") or body.get("tool_id") or ""
        name = body.get("name") or "tool"
        st = body.get("status") or "started"
        # Grok mid-tool frames stay kind=tool_call with status=update — treat as update
        # so we do not re-enter _on_tool_call mount path unnecessarily.
        su = "tool_call" if st in ("started", "pending", "running", "") else "tool_call_update"
        return _frame(
            su,
            {
                "toolCallId": str(tid) if tid is not None else "",
                "title": name,
                "name": name,
                "rawInput": body.get("args") or body.get("input"),
                "status": st,
            },
        )

    if kind == "tool_result":
        tid = body.get("tool_id") or body.get("id") or ""
        raw = body.get("content") or text or ""
        decoded = _decode_tool_output(raw)
        # Shape matching grok tool_call_update so _on_tool_call_update
        # can find content[].content.text and extract interjections.
        return _frame(
            "tool_call_update",
            {
                "toolCallId": str(tid) if tid is not None else "",
                "status": body.get("status") or "completed",
                "rawOutput": decoded,
                "content": [
                    {
                        "type": "content",
                        "content": {"type": "text", "text": decoded},
                    }
                ],
            },
        )

    # meta / usage / raw_only — no paint
    return None



def iter_hot_tui_events_from_offset(
    path: Path, offset: int
) -> tuple[list[dict[str, Any]], int]:
    """Read complete hot.jsonl lines from offset → TUI update events + new offset."""
    from aa_stream_parser import iter_aa_stream_lines_from_offset

    raw, new_off = iter_aa_stream_lines_from_offset(str(path), offset)
    out = []
    for ev in raw:
        u = aa_event_to_tui_update(ev)
        if u is not None:
            out.append(u)
    return out, new_off


SPEECH_SESSION_UPDATES = frozenset({
    "user_message_chunk",
    "agent_message_chunk",
    "agent_thought_chunk",
})


def is_speech_tui_event(event: dict[str, Any]) -> bool:
    """True if a TUI update event carries visible speech/thought text."""
    if not isinstance(event, dict):
        return False
    update = (event.get("params") or {}).get("update") or {}
    su = update.get("sessionUpdate") or ""
    if su not in SPEECH_SESSION_UPDATES:
        return False
    c = update.get("content") or {}
    t = c.get("text", "") if isinstance(c, dict) else (c if isinstance(c, str) else "")
    return bool(str(t).strip())





def cheap_hot_line_speech(line: str) -> Optional[tuple[str, str, str]]:
    """Fast path for lazy-load: (sessionUpdate, text, role-ish) without full json.loads.

    Fat aa.stream lines embed huge native{} blobs; json.loads on every line
    spikes CPU and holds loading_history so scroll cannot advance.
    Returns None if not dialogue/chrome speech.
    """
    import re
    if not line or "\"body\"" not in line:
        return None
    # role
    rm = re.search(r'"role"\s*:\s*"(user|assistant|human|agent)"', line)
    if not rm:
        return None
    role = rm.group(1)
    # body.kind text?
    if '"kind":"text"' not in line and '"kind": "text"' not in line:
        if '"kind":"text_delta"' not in line and '"kind": "text_delta"' not in line:
            return None
    # body text — prefer "body":{... "text":"..."}
    tm = re.search(
        r'"body"\s*:\s*\{[^{}]*?"text"\s*:\s*"((?:\\.|[^"\\])*)"',
        line,
    )
    if not tm:
        # fallback shorter
        tm = re.search(r'"text"\s*:\s*"((?:\\.|[^"\\]){1,4000})"', line)
    if not tm:
        return None
    text = (
        tm.group(1)
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )
    if not text.strip():
        return None
    if role in ("user", "human"):
        su = "user_message_chunk"
    else:
        su = "agent_message_chunk"
    return su, text, role


def is_chrome_speech(text: str) -> bool:
    """System continue / session-limit / context-left — not real dialogue.

    Lazy-load must not spend its speech budget on these or scroll sticks
    in a multi-MB wall of continues after a usage limit.
    """
    if not text or not isinstance(text, str):
        return True
    s = text.strip()
    low = s.lower()
    if low.startswith("[continue"):
        return True
    if "your turn ended" in low and "stand by" in low:
        return True
    if "session limit" in low or "you've hit your session limit" in low:
        return True
    if low.startswith("[context left") or "| compaction" in low:
        return True
    if low.startswith("[aa.control]"):
        return True
    return False


def is_dialogue_speech(event: dict) -> bool:
    """Speech worth counting toward lazy-load / -t N targets."""
    if not is_speech_tui_event(event):
        return False
    update = (event.get("params") or {}).get("update") or {}
    c = update.get("content") or {}
    text = c.get("text", "") if isinstance(c, dict) else (c if isinstance(c, str) else "")
    return not is_chrome_speech(str(text))


def _thin_event_from_fat_line(line: str) -> Optional[dict[str, Any]]:
    """Paint-able tool update from an oversized hot line without full json.loads.

    Fat run_terminal_command completions embed full stdout + byte-array rawOutput
    (often 60–200KB). Skipping them left empty ✓ title-only panels.
    """
    import re

    if "tool_call" not in line and "tool_result" not in line:
        return None

    def _s(pat: str, default: str = "") -> str:
        m = re.search(pat, line)
        return m.group(1) if m else default

    tool_id = _s(r'"toolCallId"\s*:\s*"([^"]+)"') or _s(
        r'"id"\s*:\s*"(call-[^"]+)"'
    )
    status = _s(
        r'"status"\s*:\s*"(completed|failed|in_progress|running)"', "completed"
    )
    title = _s(r'"title"\s*:\s*"((?:\\.|[^"\\])*)"')
    if title:
        title = title.replace("\\n", " ").replace('\\"', '"')[:120]

    text = ""
    # Prefer content text block (human stdout), not byte arrays
    m2 = re.search(
        r'"content"\s*:\s*\{\s*"type"\s*:\s*"text"\s*,\s*"text"\s*:\s*"((?:\\.|[^"\\])*)"',
        line,
    )
    if m2:
        text = m2.group(1)
    if not text:
        m2 = re.search(
            r'"output_for_prompt"\s*:\s*"((?:\\.|[^"\\]){0,6000})"', line
        )
        if m2:
            text = m2.group(1)
    if not text:
        m2 = re.search(
            r'"body"\s*:\s*\{[^}]{0,200}?"text"\s*:\s*"((?:\\.|[^"\\])*)"', line
        )
        if m2:
            text = m2.group(1)
    if text:
        text = (
            text.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
        if len(text) > 8000:
            text = text[:8000] + "\n… [truncated fat tool line]"
    if not tool_id and not text:
        return None
    if not text:
        text = f"(output elided — line {len(line)} bytes)"
    return {
        "timestamp": None,
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_id or "unknown",
                "status": status,
                "title": title or "tool",
                "content": [
                    {
                        "type": "content",
                        "content": {"type": "text", "text": text},
                    }
                ],
                "rawOutput": text[:2000],
            }
        },
        "_aa_stream": True,
        "_aa_fat_thin": True,
    }


def line_to_tui_event(line: str, hist_kind: str = "updates") -> Optional[dict[str, Any]]:
    """Parse one jsonl line from hot or updates into a TUI dispatch event."""
    import json as _json
    if line is None:
        return None
    # Cheap length gate BEFORE full json.loads (1.4MB lines freeze the TUI).
    # Tool *completions* are often fat (stdout + byte-array rawOutput) but still
    # need a panel body — thin-extract instead of dropping → empty ✓ title-only.
    if len(line) > MAX_TUI_LINE_BYTES:
        thin = _thin_event_from_fat_line(line)
        if thin is not None:
            return thin
        return None
    try:
        obj = _json.loads(line)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    if hist_kind == "hot" or (
        obj.get("format") == "aa.stream" or (obj.get("body") and obj.get("class"))
    ):
        return aa_event_to_tui_update(obj)
    # already grok-shaped
    if (obj.get("params") or {}).get("update") is not None:
        return obj
    return None



def _line_start_before(data: bytes, end: int) -> int:
    """Start offset of the line that ends at end (end is exclusive, at newline or EOF)."""
    if end <= 0:
        return 0
    # end points at first byte of NEXT line (or EOF). Previous byte should be \n
    # or we are mid-file after a forced jump.
    nl = data.rfind(b"\n", 0, end - 1 if end > 0 else 0)
    if nl < 0:
        return 0
    return nl + 1


def scan_older_history_events(
    path_str: str,
    earliest_offset: int,
    hist_kind: str = "hot",
    *,
    speech_target: int = 25,
    max_bytes: int = 12 * 1024 * 1024,
    window: int = 512 * 1024,
    max_tool_panels: int = 4,
    max_line_skip: int = 8 * 1024 * 1024,
) -> dict:
    """Walk hot/updates backward from earliest_offset; return plain event dicts.

    Critical: lines longer than ``window`` must not stall the cursor. A naive
    seek+readline alignment lands on the same newline forever when a fat tool
    line spans the whole window — CPU spins, loading_history sticks, scroll
    cannot advance past that point (Astro "Confirmed the side effect" wall).
    """
    from pathlib import Path as _Path

    updates_path = _Path(path_str)
    speech_n = 0
    tool_n = 0
    chrome_n = 0
    new_earliest = earliest_offset
    bytes_read = 0
    cursor = earliest_offset
    collected: list[tuple[int, dict]] = []
    iterations = 0
    max_iterations = max(64, (earliest_offset // max(1, window)) + 32)

    while (
        cursor > 0
        and speech_n < speech_target
        and bytes_read < max_bytes
        and iterations < max_iterations
    ):
        iterations += 1
        read_size = min(cursor, window)
        seek_pos = cursor - read_size
        with open(updates_path, "rb") as f:
            f.seek(seek_pos)
            if seek_pos > 0:
                f.readline()  # align to next full line
            data_start = f.tell()
            # Fat line spans the whole window: readline lands at/after cursor.
            if data_start >= cursor:
                # Skip the fat line ending at cursor by finding its start.
                back = min(cursor, max_line_skip)
                f.seek(cursor - back)
                blob = f.read(back)
                # blob ends at cursor; line before cursor starts after last \n
                nl = blob.rfind(b"\n", 0, len(blob) - 1 if len(blob) else 0)
                if nl < 0:
                    # still inside one giant line — jump back by `back`
                    line_start = cursor - back
                else:
                    line_start = (cursor - back) + nl + 1
                skipped = cursor - line_start
                bytes_read += max(skipped, 1)  # ensure max_bytes can fire
                new_earliest = min(new_earliest, line_start)
                cursor = line_start
                if cursor <= 0:
                    new_earliest = 0
                    break
                continue

            raw = f.read(cursor - data_start)
        bytes_read += len(raw)
        if not raw:
            # Empty but data_start < cursor — treat as no progress
            if seek_pos <= 0:
                new_earliest = 0
                break
            bytes_read += 1
            cursor = data_start if data_start < cursor else seek_pos
            continue

        batch: list[tuple[int, str]] = []
        pos = data_start
        parts = raw.split(b"\n")
        for i, lb in enumerate(parts):
            line_start = pos
            blen = len(lb) + (1 if i < len(parts) - 1 else 0)
            pos += blen
            if not lb.strip() or line_start >= earliest_offset:
                continue
            if len(lb) > 2 * 1024 * 1024:
                continue
            try:
                batch.append((line_start, lb.decode("utf-8", errors="replace")))
            except Exception:
                continue

        for line_start, line in reversed(batch):
            if hist_kind == "hot":
                hit = cheap_hot_line_speech(line)
                if hit is not None:
                    su, text, _role = hit
                    # Do not mount chrome — it burns UI and confuses scroll walls.
                    new_earliest = line_start
                    if is_chrome_speech(text):
                        chrome_n += 1
                        continue
                    event = {
                        "params": {
                            "update": {
                                "sessionUpdate": su,
                                "content": {"text": text},
                            }
                        }
                    }
                    collected.append((line_start, event))
                    speech_n += 1
                    if speech_n >= speech_target:
                        break
                    continue

            # Fallback: tools / updates kind
            if len(line) > 64 * 1024 and "tool_call" not in line and "tool_result" not in line:
                continue
            event = line_to_tui_event(line, hist_kind)
            if not event:
                continue
            update = (event.get("params") or {}).get("update") or {}
            et = update.get("sessionUpdate", "")
            if et in (
                "user_message_chunk",
                "agent_message_chunk",
                "agent_thought_chunk",
            ):
                c = update.get("content") or {}
                text = c.get("text", "") if isinstance(c, dict) else ""
                if not str(text).strip():
                    continue
                new_earliest = line_start
                if is_chrome_speech(str(text)):
                    chrome_n += 1
                    continue
                collected.append((line_start, event))
                speech_n += 1
                if speech_n >= speech_target:
                    break
            elif et in ("tool_call", "tool_call_update") and tool_n < max_tool_panels:
                collected.append((line_start, event))
                tool_n += 1
                new_earliest = min(new_earliest, line_start)

        cursor = data_start
        if data_start <= 0:
            new_earliest = 0
            break

    collected.sort(key=lambda t: t[0])
    return {
        "events": [e for _, e in collected],
        "new_earliest": max(0, int(new_earliest)),
        "speech_n": speech_n,
        "chrome_n": chrome_n,
        "tool_n": tool_n,
        "bytes_read": bytes_read,
        "iterations": iterations,
    }



# Session meta that bloats -t catch-up without helping the operator read the tip.
_TIP_DROP_SESSION_UPDATES = frozenset({
    "task_completed",
    "task_backgrounded",
    "background_tasks",
    "turn_completed",
    "auto_compact_started",
    "auto_compact_completed",
    "compaction_checkpoint",
    "memory_dream_queued",
    "memory_dream_started",
    "memory_dream_completed",
    "hook_execution",
})


def thin_tip_events(
    events: list[dict[str, Any]],
    n_speech: int,
    *,
    speech_first: bool = True,
) -> list[dict[str, Any]]:
    """Catch-up tip: last N *dialogue* speeches + collapsed tools in that span.

    ``-t N`` means N real user/agent lines, not N paint widgets. Keeping every
    tool_call + tool_call_update + task_* meta inside the speech span turned
    ``-t50`` into ~60 speech labels and 300+ events. Collapse to one tool panel
    per toolCallId (last state wins) and drop pure session meta.
    """
    if not events:
        return []
    if n_speech and n_speech > 0:
        span = select_tail_events(events, n_speech, speech_first=speech_first)
    else:
        span = list(events)

    out: list[dict[str, Any]] = []
    # toolCallId -> index in out (for in-place replace)
    tool_idx: dict[str, int] = {}

    def _tool_id(update: dict) -> str:
        return str(
            update.get("toolCallId")
            or update.get("tool_call_id")
            or update.get("id")
            or ""
        )

    for ev in span:
        update = (ev.get("params") or {}).get("update") or {}
        et = update.get("sessionUpdate", "") or ""
        if et in _TIP_DROP_SESSION_UPDATES:
            continue
        # Continues / session-limit / aa.control: not tip dialogue
        if et in (
            "user_message_chunk",
            "agent_message_chunk",
            "agent_thought_chunk",
        ):
            c = update.get("content") or {}
            text = c.get("text", "") if isinstance(c, dict) else (c if isinstance(c, str) else "")
            if is_chrome_speech(str(text)):
                continue
        if et in ("tool_call", "tool_call_update"):
            tid = _tool_id(update) or f"anon:{id(ev)}"
            if tid in tool_idx:
                out[tool_idx[tid]] = ev  # last state wins
            else:
                tool_idx[tid] = len(out)
                out.append(ev)
            continue
        out.append(ev)
    return out


def select_tail_events(
    events: list[dict[str, Any]],
    n: int,
    *,
    speech_first: bool = True,
) -> list[dict[str, Any]]:
    """Take a tail window of N items.

    When speech_first (default), N counts *speech* events (user/agent/thought
    with text). Tools inside that span are kept so context is not stripped.
    """
    if n is None or n <= 0 or len(events) <= n and not speech_first:
        return list(events)
    if not speech_first:
        return events[-n:] if len(events) > n else list(events)

    speech = 0
    start = len(events)
    for i in range(len(events) - 1, -1, -1):
        start = i
        if is_dialogue_speech(events[i]):
            speech += 1
            if speech >= n:
                break
    return events[start:]
