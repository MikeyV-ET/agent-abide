"""
Token-efficient transcript generator from V1 history/speech.jsonl (fallback: conversation.jsonl).

Source of truth remains JSONL. This module produces a plain-text view
optimized for memory agents / compaction context (minimal chrome).

KEEP kinds (post-schema): message, speech, thinking, interjection
DROP kinds: doorbell, prompt, speech_repair
Pre-schema lines (no kind): user/assistant by default; optional thinking.

Memory profile (see docs/specs/aa_stream/V1_TRANSCRIPT.md + memoryagent_1_recommendations.md):
  day headers, strip default tui wrappers, drop ack/bell chrome, bare periods,
  doing-preambles, ephact unwrap, markdown heading marker strip, optional closer drop.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, TextIO, Tuple, Union

# --- policy ---
KEEP_KINDS = frozenset({"message", "speech", "thinking", "interjection"})
DROP_KINDS = frozenset({"doorbell", "prompt", "speech_repair"})

_OPS_USER_PREFIXES = (
    "[continue ",
    "[clock ",
    "[heartbeat ",
    "[remind ",
    "[localmail ",
    "[Compaction complete",
    "[session:",
    "Your turn ended.",
    "Retry ",
)

ROLE_LABEL = {
    "user": "U",
    "assistant": "A",
    "thinking": "T",
    "system": "S",
}

PathLike = Union[str, Path]

# --- regexes ---
_CONTEXT_LEFT_RE = re.compile(
    r"\[Context left[^\]]*\]\s*", re.IGNORECASE
)
_ACK_CHROME_RE = re.compile(
    r"(?:To ack:\s*include[^\n]*|reply_via\s*=\s*\S+|"
    r"messages arrived during your (?:previous )?tool call[^\n]*)\s*",
    re.IGNORECASE,
)
_INTERJECTION_ENVELOPE_RE = re.compile(
    r"^\[interjection\s*\([^)]*\)\]\s*", re.IGNORECASE | re.MULTILINE
)
# <eric (via tui)> or [eric (via tui) (id=bell_…, ts=…)]
_ATTR_FULL_RE = re.compile(
    r"^(?:<"
    r"(?P<who1>[^>(]+)(?:\s*\(via\s+(?P<via1>[^)]+)\))?"
    r"(?:\s*\[[^\]]*\])?"  # optional [sent during …] inside <>
    r">|"
    r"\["
    r"(?P<who2>[^>(\]]+)(?:\s*\(via\s+(?P<via2>[^)]+)\))?"
    r"(?:\s*\([^)]*\))*"  # (id=…, ts=…)
    r"\])\s*",
    re.IGNORECASE,
)
_EPHACT_RE = re.compile(
    r"<ephact\b[^>]*>(.*?)</ephact>", re.IGNORECASE | re.DOTALL
)
_BARE_PERIOD_LINE_RE = re.compile(r"^\s*\.\s*$", re.MULTILINE)
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+", re.MULTILINE)
_DOING_PREAMBLE_RE = re.compile(
    r"^(Checking|Investigating|Looking|Tracing|Implementing|Reviewing|"
    r"Searching|Reading|Scanning|Orienting|Sweeping|Comparing|Acknowledging|"
    r"Exploring|Building|Wiring|Measuring|Verifying|Confirming|"
    r"Digging|Updating|Writing|Running|Starting|Restarting)\b[^\n]*\n+",
    re.IGNORECASE,
)
_LOOP_LITURGY_RE = re.compile(
    r"^.*\b(delay\s*0\s+queued|win tree synced|notes flushed|"
    r"self-loop|polling every\s+\d+\s*s|still green\.?)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
_CLOSER_HEADING_RE = re.compile(
    r"^#{1,6}\s*(Bottom line|One line|TL;DR|Summary)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# exact launch one-liners to dedupe (normalized)
_CMD_LINE_RE = re.compile(
    r"^(?:```(?:bash|sh|shell)?\n)?\s*((?:cd\s+\S+\s+&&\s+)?(?:python3?|bash|npm|pnpm|yarn|cargo|go|uv)\s+[^\n]+)\s*(?:\n```)?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class TranscriptOptions:
    include_thinking: bool = False
    include_system: bool = False
    drop_ops_user: bool = True
    merge_consecutive: bool = True
    role_style: str = "short"  # short | long | none
    ts_style: str = "none"  # none | time | iso — per-entry; prefer day_headers for memory
    strip_attribution: bool = False
    # If strip_attribution: keep a short channel tag when via != default_channel
    default_channel: str = "tui"
    max_entry_chars: int = 0
    separator: str = "\n"

    # --- memory-oriented chrome cuts (memoryagent_1_recommendations) ---
    day_headers: bool = False
    drop_context_left: bool = False
    drop_ack_chrome: bool = False
    drop_bare_periods: bool = False
    drop_doing_preamble: bool = False
    unwrap_ephact: bool = False
    drop_loop_liturgy: bool = False
    dedupe_exact_commands: bool = False
    # Markdown ## headers: "strip_markers" keeps text, drops #; "drop_closers" removes Bottom line etc.; "keep"
    md_headings: str = "keep"  # keep | strip_markers | strip_and_drop_closers
    drop_closer_headings: bool = False  # ### Bottom line / One line lines only

    @classmethod
    def memory_pack(cls) -> "TranscriptOptions":
        """Defaults for sticky memory ingest (es recommendations)."""
        return cls(
            include_thinking=False,
            drop_ops_user=True,
            merge_consecutive=True,
            role_style="short",
            ts_style="none",
            strip_attribution=True,
            default_channel="tui",
            day_headers=True,
            drop_context_left=True,
            drop_ack_chrome=True,
            drop_bare_periods=True,
            drop_doing_preamble=True,
            unwrap_ephact=True,
            drop_loop_liturgy=True,
            dedupe_exact_commands=True,
            md_headings="single_hash",
            drop_closer_headings=True,
        )


@dataclass
class TranscriptStats:
    source_lines: int = 0
    kept_entries: int = 0
    dropped_kind: int = 0
    dropped_ops: int = 0
    dropped_empty: int = 0
    dropped_role: int = 0
    merged_blocks: int = 0
    chars_out: int = 0
    chars_in_kept: int = 0
    day_headers_emitted: int = 0
    commands_deduped: int = 0

    @property
    def approx_tokens_out(self) -> int:
        return max(0, (self.chars_out + 3) // 4)

    @property
    def approx_tokens_in_kept(self) -> int:
        return max(0, (self.chars_in_kept + 3) // 4)


@dataclass
class TranscriptResult:
    text: str
    stats: TranscriptStats
    entries: List[Dict[str, Any]] = field(default_factory=list)


def _content_of(obj: dict) -> str:
    c = obj.get("content")
    if c is None:
        c = obj.get("text") or obj.get("message") or ""
    if isinstance(c, list):
        parts = []
        for block in c:
            if isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        return "\n".join(parts)
    if not isinstance(c, str):
        return json.dumps(c, ensure_ascii=False)
    return c


def _is_ops_user(text: str) -> bool:
    s = text.strip()
    if not s:
        return True
    for p in _OPS_USER_PREFIXES:
        if s.startswith(p):
            return True
    if s == "Your turn ended. You may continue, delay, or stand by.":
        return True
    return False


def _parse_ts(obj: dict) -> Optional[float]:
    ts = obj.get("ts") or obj.get("timestamp")
    if ts is None:
        # try context-left footer date
        c = _content_of(obj)
        m = re.search(
            r"\|\s*([A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}\s+[A-Z]{2,4}\s+\d{4})\s*\]",
            c,
        )
        if m:
            try:
                # Fri Sep 11 22:37 PDT 2026 — %Z often fails; strip tz name
                raw = m.group(1)
                raw2 = re.sub(r"\s+[A-Z]{2,5}\s+", " ", raw)
                return datetime.strptime(raw2, "%a %b %d %H:%M %Y").timestamp()
            except ValueError:
                pass
        return None
    if isinstance(ts, (int, float)):
        t = float(ts)
        return t / 1000.0 if t > 1e12 else t
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _fmt_ts(ts: Optional[float], style: str) -> str:
    if style == "none" or ts is None:
        return ""
    try:
        dt = datetime.fromtimestamp(ts)
    except (OSError, ValueError, OverflowError):
        return ""
    if style == "time":
        return dt.strftime("%H:%M")
    if style == "iso":
        return dt.isoformat(timespec="seconds")
    return ""


def _day_key(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts).strftime("%a %b %d %Y")
    except (OSError, ValueError, OverflowError):
        return None


def _role_label(role: str, style: str) -> str:
    if style == "none":
        return ""
    if style == "long":
        return role
    return ROLE_LABEL.get(role, role[:1].upper() or "?")


def _strip_attribution(text: str, default_channel: str) -> Tuple[str, Optional[str]]:
    """Remove eric wrappers. Returns (text, via_channel_if_non_default)."""
    t = text.strip()
    via_out: Optional[str] = None
    for _ in range(4):
        m = _ATTR_FULL_RE.match(t)
        if not m:
            break
        via = (m.group("via1") or m.group("via2") or "").strip().lower()
        if via and via != default_channel.lower():
            via_out = via
        t = t[m.end() :].strip()
    return t, via_out


def clean_content(text: str, opt: TranscriptOptions, stats: Optional[TranscriptStats] = None) -> str:
    """Apply chrome cuts to a single entry body."""
    t = text

    if opt.drop_context_left:
        t = _CONTEXT_LEFT_RE.sub("", t)

    if opt.drop_ack_chrome:
        t = _INTERJECTION_ENVELOPE_RE.sub("", t)
        t = _ACK_CHROME_RE.sub("", t)

    if opt.unwrap_ephact:
        t = _EPHACT_RE.sub(lambda m: m.group(1).strip(), t)

    if opt.drop_bare_periods:
        t = _BARE_PERIOD_LINE_RE.sub("", t)

    if opt.drop_loop_liturgy:
        t = _LOOP_LITURGY_RE.sub("", t)

    if opt.drop_doing_preamble:
        # only first line if it matches doing-verb
        t2 = _DOING_PREAMBLE_RE.sub("", t, count=1)
        # only drop if real body remains (not a one-line "Checking X." turn)
        if len(t2.strip()) >= 20:
            t = t2

    if opt.drop_closer_headings:
        t = _CLOSER_HEADING_RE.sub("", t)

    if opt.md_headings == "single_hash":
        # ## / ### / … → single # (es: one hash marks header; more is waste)
        t = _MD_HEADING_RE.sub("# ", t)
    elif opt.md_headings == "strip_markers":
        t = _MD_HEADING_RE.sub("", t)
    elif opt.md_headings == "strip_and_drop_closers":
        t = _CLOSER_HEADING_RE.sub("", t)
        t = _MD_HEADING_RE.sub("", t)

    # collapse excess blank lines
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def iter_raw_entries(stream: Iterable[str]) -> Iterator[dict]:
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict):
            yield o


def should_keep(obj: dict, opt: TranscriptOptions) -> Tuple[bool, str]:
    kind = obj.get("kind")
    role = obj.get("role") or ""

    if kind in DROP_KINDS:
        return False, "kind"
    if kind is not None and kind not in KEEP_KINDS and kind != "":
        if role not in ("user", "assistant", "thinking"):
            return False, "kind"

    if role == "thinking" and not opt.include_thinking:
        if kind == "thinking" or kind is None:
            return False, "role"
    if role == "system" and not opt.include_system:
        return False, "role"
    if role not in ("user", "assistant", "thinking", "system"):
        return False, "role"

    content = _content_of(obj).strip()
    if not content:
        return False, "empty"

    if opt.drop_ops_user and role == "user":
        if kind != "interjection" and _is_ops_user(content):
            return False, "ops"
        # also after stripping context-left
        stripped = _CONTEXT_LEFT_RE.sub("", content).strip()
        if kind != "interjection" and _is_ops_user(stripped):
            return False, "ops"

    return True, ""


def normalize_entry(obj: dict, opt: TranscriptOptions) -> Dict[str, Any]:
    role = obj.get("role") or "user"
    content = _content_of(obj).rstrip()
    via = None
    if opt.strip_attribution and role == "user":
        content, via = _strip_attribution(content, opt.default_channel)
    content = clean_content(content, opt)
    if opt.max_entry_chars and len(content) > opt.max_entry_chars:
        content = content[: opt.max_entry_chars - 1] + "…"
    return {
        "role": role,
        "content": content,
        "kind": obj.get("kind"),
        "ts": _parse_ts(obj),
        "session_id": obj.get("session_id"),
        "msg_id": obj.get("msg_id"),
        "seq": obj.get("seq"),
        "via": via,
    }


def merge_entries(entries: List[Dict[str, Any]], opt: TranscriptOptions) -> List[Dict[str, Any]]:
    if not opt.merge_consecutive or not entries:
        return entries
    out: List[Dict[str, Any]] = []
    cur = dict(entries[0])
    for e in entries[1:]:
        if e["role"] == cur["role"] and e.get("via") == cur.get("via"):
            cur["content"] = cur["content"].rstrip() + "\n" + e["content"].lstrip()
        else:
            out.append(cur)
            cur = dict(e)
    out.append(cur)
    return out


def _dedupe_commands_in_text(text: str, seen: set, stats: TranscriptStats) -> str:
    """Drop exact command lines already seen in the transcript."""
    lines = text.split("\n")
    out = []
    for line in lines:
        key = line.strip()
        # simple command-ish lines
        is_cmd = bool(
            re.match(
                r"^(cd\s+\S+|python3?\s+|bash\s+|npm\s+|pnpm\s+|cargo\s+|uv\s+|go\s+)",
                key,
            )
        ) or (key.startswith("`") and len(key) < 200)
        if is_cmd and key in seen:
            stats.commands_deduped += 1
            continue
        if is_cmd and key:
            seen.add(key)
        out.append(line)
    return "\n".join(out)


def format_entries(
    entries: Sequence[Dict[str, Any]],
    opt: TranscriptOptions,
    stats: TranscriptStats,
) -> str:
    blocks: List[str] = []
    last_day: Optional[str] = None
    seen_cmds: set = set()

    for e in entries:
        content = e["content"]
        if not content.strip():
            continue

        if opt.dedupe_exact_commands:
            content = _dedupe_commands_in_text(content, seen_cmds, stats)
            if not content.strip():
                continue

        if opt.day_headers:
            dk = _day_key(e.get("ts"))
            if dk and dk != last_day:
                blocks.append(f"--- {dk} ---")
                stats.day_headers_emitted += 1
                last_day = dk

        label = _role_label(e["role"], opt.role_style)
        if e.get("via") and opt.strip_attribution:
            # non-default channel
            if label:
                label = f"{label} ({e['via']})"
            else:
                label = e["via"]
        ts = _fmt_ts(e.get("ts"), opt.ts_style)
        header_bits = [b for b in (ts, label) if b]
        if header_bits:
            h = " ".join(header_bits)
            if "\n" in content:
                blocks.append(f"{h}:\n{content}")
            else:
                blocks.append(f"{h}: {content}")
        else:
            blocks.append(content)

    return opt.separator.join(blocks)


def generate_transcript(
    source: Union[PathLike, Iterable[str], TextIO],
    opt: Optional[TranscriptOptions] = None,
    *,
    return_entries: bool = False,
) -> TranscriptResult:
    opt = opt or TranscriptOptions()
    stats = TranscriptStats()

    if isinstance(source, (str, Path)):
        path = Path(source)
        f = path.open("r", encoding="utf-8", errors="replace")
        close = True
        stream: Iterable[str] = f
    else:
        f = None
        close = False
        stream = source  # type: ignore

    kept: List[Dict[str, Any]] = []
    try:
        for obj in iter_raw_entries(stream):
            stats.source_lines += 1
            ok, reason = should_keep(obj, opt)
            if not ok:
                if reason == "kind":
                    stats.dropped_kind += 1
                elif reason == "ops":
                    stats.dropped_ops += 1
                elif reason == "empty":
                    stats.dropped_empty += 1
                elif reason == "role":
                    stats.dropped_role += 1
                continue
            ent = normalize_entry(obj, opt)
            if not ent["content"].strip():
                stats.dropped_empty += 1
                continue
            stats.chars_in_kept += len(ent["content"])
            kept.append(ent)
    finally:
        if close and f is not None:
            f.close()

    before_merge = len(kept)
    kept = merge_entries(kept, opt)
    stats.merged_blocks = max(0, before_merge - len(kept))
    stats.kept_entries = len(kept)
    text = format_entries(kept, opt, stats)
    if text and not text.endswith("\n"):
        text += "\n"
    stats.chars_out = len(text)

    return TranscriptResult(
        text=text,
        stats=stats,
        entries=kept if return_entries else [],
    )


def generate_from_agent(
    agent_name: str,
    opt: Optional[TranscriptOptions] = None,
    *,
    agents_home: Optional[Path] = None,
) -> TranscriptResult:
    home = agents_home or Path.home() / "agents"
    candidates = [
        home / agent_name / "asdaaas" / "history" / "speech.jsonl",
        home / "LeviSmith" / agent_name / "asdaaas" / "history" / "speech.jsonl",
        home / agent_name / "asdaaas" / "conversation.jsonl",
        home / "LeviSmith" / agent_name / "asdaaas" / "conversation.jsonl",
    ]
    for p in candidates:
        if p.is_file():
            return generate_transcript(p, opt)
    raise FileNotFoundError(
        f"V1 speech journal not found for {agent_name}; tried {candidates}"
    )


def stats_summary(stats: TranscriptStats) -> str:
    return (
        f"source_lines={stats.source_lines} kept={stats.kept_entries} "
        f"drop(kind={stats.dropped_kind} ops={stats.dropped_ops} "
        f"role={stats.dropped_role} empty={stats.dropped_empty}) "
        f"merged={stats.merged_blocks} days={stats.day_headers_emitted} "
        f"cmd_dedupe={stats.commands_deduped} "
        f"chars_out={stats.chars_out} (~{stats.approx_tokens_out} tok)"
    )


# ---------------------------------------------------------------------------
# User-only index + expand-around-turn (memory agent two-stage read)
# ---------------------------------------------------------------------------

def load_kept_entries(
    source: Union[PathLike, Iterable[str], TextIO],
    opt: Optional[TranscriptOptions] = None,
    *,
    merge: Optional[bool] = None,
) -> Tuple[List[Dict[str, Any]], TranscriptStats]:
    """Load filtered/normalized entries. Default: no merge (stable turn boundaries)."""
    opt = opt or TranscriptOptions.memory_pack()
    if merge is None:
        merge = False
    # copy options with merge override
    opt2 = TranscriptOptions(**{**opt.__dict__, "merge_consecutive": merge})
    stats = TranscriptStats()
    if isinstance(source, (str, Path)):
        f = Path(source).open("r", encoding="utf-8", errors="replace")
        close = True
        stream: Iterable[str] = f
    else:
        f = None
        close = False
        stream = source  # type: ignore
    kept: List[Dict[str, Any]] = []
    try:
        for obj in iter_raw_entries(stream):
            stats.source_lines += 1
            ok, reason = should_keep(obj, opt2)
            if not ok:
                if reason == "kind":
                    stats.dropped_kind += 1
                elif reason == "ops":
                    stats.dropped_ops += 1
                elif reason == "empty":
                    stats.dropped_empty += 1
                elif reason == "role":
                    stats.dropped_role += 1
                continue
            ent = normalize_entry(obj, opt2)
            if not ent["content"].strip():
                stats.dropped_empty += 1
                continue
            stats.chars_in_kept += len(ent["content"])
            kept.append(ent)
    finally:
        if close and f is not None:
            f.close()
    if merge:
        before = len(kept)
        kept = merge_entries(kept, opt2)
        stats.merged_blocks = max(0, before - len(kept))
    stats.kept_entries = len(kept)
    return kept, stats


def resolve_conversation_path(
    agent_name: Optional[str] = None,
    path: Optional[PathLike] = None,
    *,
    agents_home: Optional[Path] = None,
) -> Path:
    if path is not None:
        return Path(path)
    if not agent_name:
        raise FileNotFoundError("need agent_name or path")
    home = agents_home or Path.home() / "agents"
    candidates = [
        home / agent_name / "asdaaas" / "history" / "speech.jsonl",
        home / "LeviSmith" / agent_name / "asdaaas" / "history" / "speech.jsonl",
        home / agent_name / "asdaaas" / "conversation.jsonl",
        home / "LeviSmith" / agent_name / "asdaaas" / "conversation.jsonl",
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError(f"V1 speech journal not found; tried {candidates}")


def build_user_index(
    entries: Sequence[Dict[str, Any]],
    *,
    snippet_chars: int = 160,
) -> List[Dict[str, Any]]:
    """
    Eric-turn index with stable handles.

      u{n}  — 0-based among user turns
      e{n}  — index into full kept entry list
      ts    — unix seconds when available

    Each row keeps full `content` plus a short `snippet` (for compact listings).
    """
    index: List[Dict[str, Any]] = []
    u_i = 0
    for e_i, e in enumerate(entries):
        if e.get("role") != "user":
            continue
        ts = e.get("ts")
        content = (e.get("content") or "").strip()
        one_line = content.replace("\n", " ")
        if snippet_chars and len(one_line) > snippet_chars:
            snippet = one_line[: snippet_chars - 1] + "…"
        else:
            snippet = one_line
        day = _day_key(ts) if ts is not None else None
        index.append({
            "id": f"u{u_i}",
            "u": u_i,
            "e": e_i,
            "ts": ts,
            "ts_iso": _fmt_ts(ts, "iso") if ts is not None else None,
            "day": day,
            "via": e.get("via"),
            "kind": e.get("kind"),
            "content": content,
            "snippet": snippet,
            "chars": len(content),
        })
        u_i += 1
    return index


def format_user_index(
    index: Sequence[Dict[str, Any]],
    *,
    with_header: bool = True,
    full_content: bool = True,
    show_meta: bool = False,
    day_headers: bool = True,
) -> str:
    """Format user index as text. Default: full bodies, lean uN headers, day banners."""
    nl = chr(10)
    lines: List[str] = []
    if with_header:
        mode = "full" if full_content else "snippet"
        lines.append("# user turns: %d (body=%s)" % (len(index), mode))
    last_day = None
    for row in index:
        day = row.get("day")
        if day_headers and day and day != last_day:
            lines.append("--- %s ---" % day)
            last_day = day
        if show_meta:
            ts_bit = ""
            if row.get("ts") is not None:
                ts_bit = " ts=%.3f" % float(row["ts"])
                if row.get("ts_iso"):
                    ts_bit += " (%s)" % row["ts_iso"]
            via = (" via=%s" % row["via"]) if row.get("via") else ""
            header = "%s e=%s%s%s" % (row["id"], row["e"], ts_bit, via)
        else:
            header = str(row["id"])
        if full_content:
            body = (row.get("content") or row.get("snippet") or "").rstrip()
        else:
            body = (row.get("snippet") or row.get("content") or "").rstrip()
            body = body.replace(nl, " ")
        if full_content and nl in body:
            lines.append("%s:" % header)
            lines.append(body)
            lines.append("")
        else:
            lines.append("%s: %s" % (header, body))
            if full_content:
                lines.append("")
    text = nl.join(lines)
    if text and not text.endswith(nl):
        text += nl
    return text



def user_search(
    index: Sequence[Dict[str, Any]],
    query: str,
    *,
    limit: int = 50,
    case_insensitive: bool = True,
) -> List[Dict[str, Any]]:
    if not query:
        return list(index)[:limit]
    q = query.lower() if case_insensitive else query
    hits = []
    for row in index:
        hay = row.get("snippet") or ""
        if case_insensitive:
            hay = hay.lower()
        if q in hay:
            hits.append(row)
            if len(hits) >= limit:
                break
    return hits


def _resolve_anchor(
    entries: Sequence[Dict[str, Any]],
    index: Sequence[Dict[str, Any]],
    *,
    user_index: Optional[int] = None,
    entry_index: Optional[int] = None,
    ts: Optional[float] = None,
    handle: Optional[str] = None,
) -> int:
    if handle:
        h = handle.strip()
        if h.startswith("u") and h[1:].isdigit():
            user_index = int(h[1:])
        elif h.startswith("e") and h[1:].isdigit():
            entry_index = int(h[1:])
        elif h.startswith("ts="):
            ts = float(h[3:])
        elif h.isdigit():
            user_index = int(h)
        else:
            raise ValueError(f"unknown handle: {handle}")

    if entry_index is not None:
        if entry_index < 0 or entry_index >= len(entries):
            raise IndexError(f"entry_index {entry_index} out of range")
        return entry_index

    if user_index is not None:
        if user_index < 0 or user_index >= len(index):
            raise IndexError(f"user_index {user_index} out of range")
        return int(index[user_index]["e"])

    if ts is not None:
        best_e = None
        best_d = None
        for row in index:
            if row.get("ts") is None:
                continue
            d = abs(float(row["ts"]) - float(ts))
            if best_d is None or d < best_d:
                best_d = d
                best_e = int(row["e"])
        if best_e is None:
            for i, e in enumerate(entries):
                if e.get("ts") is None:
                    continue
                d = abs(float(e["ts"]) - float(ts))
                if best_d is None or d < best_d:
                    best_d = d
                    best_e = i
        if best_e is None:
            raise ValueError("no entries with timestamps to match ts=")
        return best_e

    raise ValueError("need handle, user_index, entry_index, or ts")


def expand_around(
    entries: Sequence[Dict[str, Any]],
    *,
    user_index: Optional[int] = None,
    entry_index: Optional[int] = None,
    ts: Optional[float] = None,
    handle: Optional[str] = None,
    before: int = 1,
    after: int = 2,
    include_thinking: bool = False,
    opt: Optional[TranscriptOptions] = None,
) -> TranscriptResult:
    """
    Full interaction window around a user turn.

    before/after = number of *user turns* before/after the anchor user turn.
    Includes the assistant block immediately preceding the window start when present.
    """
    opt = opt or TranscriptOptions.memory_pack()
    index = build_user_index(entries)
    anchor_e = _resolve_anchor(
        entries, index,
        user_index=user_index, entry_index=entry_index, ts=ts, handle=handle,
    )

    if entries[anchor_e].get("role") == "user":
        user_positions = [row["e"] for row in index]
        u_pos = user_positions.index(anchor_e)
        u_start = max(0, u_pos - before)
        u_end = min(len(user_positions) - 1, u_pos + after)
        e_lo = user_positions[u_start]
        # include immediately preceding assistant/thinking block
        if e_lo > 0 and entries[e_lo - 1].get("role") in ("assistant", "thinking"):
            e_lo -= 1
            while e_lo > 0 and entries[e_lo - 1].get("role") in ("assistant", "thinking"):
                # only pull continuous A/T block
                if entries[e_lo - 1].get("role") != entries[e_lo].get("role") and entries[e_lo].get("role") == "thinking":
                    e_lo -= 1
                    continue
                if entries[e_lo - 1].get("role") == "assistant" and entries[e_lo].get("role") == "assistant":
                    e_lo -= 1
                    continue
                break
        if u_end + 1 < len(user_positions):
            e_hi = user_positions[u_end + 1]
        else:
            e_hi = len(entries)
        window = list(entries[e_lo:e_hi])
    else:
        e_lo = max(0, anchor_e - max(before, 0))
        e_hi = min(len(entries), anchor_e + max(after, 0) + 1)
        window = list(entries[e_lo:e_hi])

    if not include_thinking:
        window = [e for e in window if e.get("role") != "thinking"]

    stats = TranscriptStats()
    stats.kept_entries = len(window)
    stats.chars_in_kept = sum(len(e.get("content") or "") for e in window)
    body = format_entries(window, opt, stats)
    meta_bits = [f"anchor_e={anchor_e}", f"before={before}", f"after={after}",
                 f"entries={len(window)}", f"~tok_in={stats.approx_tokens_in_kept}"]
    if handle:
        meta_bits.insert(0, f"handle={handle}")
    elif user_index is not None:
        meta_bits.insert(0, f"u{user_index}")
    elif ts is not None:
        meta_bits.insert(0, f"ts={ts}")
    meta = "# expand " + " ".join(meta_bits) + "\n"
    # highlight anchor user content start
    text = meta + (body if body.endswith("\n") else body + "\n")
    stats.chars_out = len(text)
    return TranscriptResult(text=text, stats=stats, entries=window)
