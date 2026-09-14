"""
V2 history L1 — compressed chunk archive + manifest (+ aa.stream hot tip).

Layout (per agent home):
  {agent_home}/asdaaas/history/     # canonical (was full_stream/ until 2026-09-14)
    hot.jsonl               # AA-owned aa.stream tip (see aa_stream.py)
    hot.meta.json           # version record + tailer high-water marks
    speech.jsonl            # V1 conversational / semantic layer (new writes)
    manifest.jsonl          # one JSON object per sealed chunk (append-only)
    chunks/
      {chunk_id}.jsonl.zst  # zstd-compressed JSONL (aa.stream lines)
    config.json             # per-agent tail_grok / inotify

Legacy dir name full_stream/ is still resolved if history/ is absent (one-cycle fallback).

Hot tip is AA-owned (not backend-native). Prune never rewrites grok/claude/codex stores.
Seal-before-prune: write chunk + manifest, verify, only then rewrite hot tail.

Spec: docs/specs/aa_stream/
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:
    import zstandard as zstd
except ImportError as e:  # pragma: no cover
    raise ImportError("history L1 requires 'zstandard' (pip install zstandard)") from e

# Hot policy (Eric 2026-09-11)
HOT_MAX_BYTES = 300 * 1024 * 1024
HOT_KEEP_BYTES = 100 * 1024 * 1024
DEFAULT_ZSTD_LEVEL = 3
MANIFEST_NAME = "manifest.jsonl"
CHUNKS_DIR = "chunks"
HOT_NAME = "hot.jsonl"
SPEECH_NAME = "speech.jsonl"
HISTORY_DIRNAME = "history"
LEGACY_HISTORY_DIRNAME = "full_stream"  # pre-2026-09-14


def agent_history_dir(agent_home: Path) -> Path:
    """Canonical history dir path (may not exist yet)."""
    return Path(agent_home) / "asdaaas" / HISTORY_DIRNAME


def resolve_history_dir(agent_home: Path) -> Path:
    """Prefer asdaaas/history/; fall back to legacy full_stream/ if present.

    A bare history/ directory (e.g. created only for speech.jsonl) must not
    shadow a populated full_stream/ that still holds hot.jsonl.
    """
    base = Path(agent_home) / "asdaaas"
    hist = base / HISTORY_DIRNAME
    leg = base / LEGACY_HISTORY_DIRNAME

    def _has_hot(d: Path) -> bool:
        return d.is_dir() and (d / HOT_NAME).is_file()

    if _has_hot(hist):
        return hist
    if _has_hot(leg):
        return leg
    if hist.exists():
        return hist
    if leg.exists():
        return leg
    return hist


def agent_full_stream_dir(agent_home: Path) -> Path:
    """Deprecated name — resolves live history dir (history/ or legacy full_stream/)."""
    return resolve_history_dir(agent_home)


def speech_path(agent_home: Path) -> Path:
    """Canonical V1 speech journal under history/."""
    return agent_history_dir(agent_home) / SPEECH_NAME


def ensure_layout(fs_dir: Path) -> None:
    fs_dir = Path(fs_dir)
    (fs_dir / CHUNKS_DIR).mkdir(parents=True, exist_ok=True)
    man = fs_dir / MANIFEST_NAME
    if not man.exists():
        man.write_text("")


def _parse_ts(obj: dict) -> Optional[float]:
    v = obj.get("timestamp", obj.get("ts"))
    if v is None:
        return None
    if isinstance(v, (int, float)):
        t = float(v)
        if t > 1e12:
            t /= 1000.0
        return t
    if isinstance(v, str):
        s = v.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            return None
    return None


def iso_utc(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@dataclass
class ChunkRecord:
    """One manifest line."""
    chunk_id: str
    path: str  # relative to history dir, e.g. chunks/foo.jsonl.zst
    from_ts: Optional[float]
    until_ts: Optional[float]
    from_ts_iso: Optional[str]
    until_ts_iso: Optional[str]
    plain_bytes: int
    zstd_bytes: int
    line_count: int
    sha256_plain: str
    session_id: Optional[str] = None
    agent: Optional[str] = None
    source: str = "seal"  # seal | migrate | prune_hot
    source_path: Optional[str] = None
    sealed_at: str = ""
    zstd_level: int = DEFAULT_ZSTD_LEVEL
    format: str = "updates_jsonl_zstd_v1"
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        d = asdict(self)
        # drop empty extra
        if not d.get("extra"):
            d.pop("extra", None)
        return json.dumps(d, separators=(",", ":"), ensure_ascii=False)


def read_manifest(fs_dir: Path) -> List[ChunkRecord]:
    man = Path(fs_dir) / MANIFEST_NAME
    if not man.exists():
        return []
    out: List[ChunkRecord] = []
    for line in man.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        d.setdefault("extra", {})
        # filter known fields
        known = {f.name for f in ChunkRecord.__dataclass_fields__.values()}  # type: ignore
        out.append(ChunkRecord(**{k: v for k, v in d.items() if k in known}))
    return out


def append_manifest(fs_dir: Path, rec: ChunkRecord) -> None:
    ensure_layout(fs_dir)
    man = Path(fs_dir) / MANIFEST_NAME
    with open(man, "a", encoding="utf-8") as f:
        f.write(rec.to_json() + "\n")
        f.flush()
        os.fsync(f.fileno())


def find_line_cut_offset(path: Path, target_start: int) -> int:
    """
    Byte offset of the first full line at or after target_start.
    If target_start <= 0, return 0. If past EOF, return file size
    aligned to last complete line end (may be EOF).
    """
    path = Path(path)
    size = path.stat().st_size
    if target_start <= 0:
        return 0
    if target_start >= size:
        return size
    with open(path, "rb") as f:
        f.seek(target_start)
        # if not at start, skip partial line
        if target_start > 0:
            f.readline()
        return f.tell()


def find_prune_cut(path: Path, keep_bytes: int = HOT_KEEP_BYTES) -> int:
    """
    Offset where prefix [0:cut) may be sealed and tail [cut:] kept.
    Cut is on a line boundary such that tail ≈ keep_bytes (not smaller
    than keep_bytes unless file is smaller).
    """
    path = Path(path)
    size = path.stat().st_size
    if size <= keep_bytes:
        return 0  # nothing to prune
    # want tail of keep_bytes → cut near size - keep_bytes
    return find_line_cut_offset(path, size - keep_bytes)


def iter_lines_range(
    path: Path, start: int = 0, end: Optional[int] = None
) -> Iterator[Tuple[int, bytes]]:
    """Yield (offset_at_line_start, line_including_newline) for [start, end)."""
    path = Path(path)
    size = path.stat().st_size
    if end is None:
        end = size
    end = min(end, size)
    with open(path, "rb") as f:
        f.seek(start)
        pos = start
        while pos < end:
            line = f.readline()
            if not line:
                break
            next_pos = pos + len(line)
            if pos >= end:
                break
            # if line crosses end, still include full line only if started before end
            yield pos, line
            pos = next_pos
            if pos >= end:
                break


def seal_byte_range(
    source: Path,
    fs_dir: Path,
    *,
    start: int = 0,
    end: Optional[int] = None,
    agent: Optional[str] = None,
    session_id: Optional[str] = None,
    source_label: str = "seal",
    zstd_level: int = DEFAULT_ZSTD_LEVEL,
    chunk_id: Optional[str] = None,
) -> ChunkRecord:
    """
    Compress source[start:end) line-wise into a new chunk; append manifest.
    Does NOT modify source. end is exclusive byte offset (line-aligned preferred).
    """
    source = Path(source)
    fs_dir = Path(fs_dir)
    ensure_layout(fs_dir)
    size = source.stat().st_size
    if end is None:
        end = size
    if start < 0 or end > size or start >= end:
        raise ValueError(f"invalid range [{start}, {end}) for size {size}")

    chunk_id = chunk_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + uuid.uuid4().hex[:10]
    )
    rel_path = f"{CHUNKS_DIR}/{chunk_id}.jsonl.zst"
    abs_path = fs_dir / rel_path
    if abs_path.exists():
        raise FileExistsError(abs_path)

    cctx = zstd.ZstdCompressor(level=zstd_level)
    h = hashlib.sha256()
    plain_bytes = 0
    line_count = 0
    from_ts: Optional[float] = None
    until_ts: Optional[float] = None

    # write to temp then rename
    fd, tmp = tempfile.mkstemp(dir=str(fs_dir / CHUNKS_DIR), suffix=".zst.tmp")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        with open(tmp_path, "wb") as out_f, cctx.stream_writer(out_f) as writer:
            with open(source, "rb") as in_f:
                in_f.seek(start)
                pos = start
                while pos < end:
                    line = in_f.readline()
                    if not line:
                        break
                    # stop if we started past end (shouldn't)
                    if pos >= end:
                        break
                    # include line even if it extends slightly past end (line boundary)
                    writer.write(line)
                    h.update(line)
                    plain_bytes += len(line)
                    line_count += 1
                    try:
                        obj = json.loads(line)
                        ts = _parse_ts(obj)
                        if ts is not None:
                            if from_ts is None:
                                from_ts = ts
                            until_ts = ts
                    except Exception:
                        pass
                    pos += len(line)
                    if pos >= end:
                        break
            writer.flush(zstd.FLUSH_FRAME)
        # fsync chunk
        with open(tmp_path, "rb+") as out_f:
            out_f.flush()
            os.fsync(out_f.fileno())
        os.replace(tmp_path, abs_path)
        # fsync dir
        dir_fd = os.open(str(fs_dir / CHUNKS_DIR), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise

    zstd_bytes = abs_path.stat().st_size
    # verify roundtrip size / hash
    dctx = zstd.ZstdDecompressor()
    plain = dctx.decompress(abs_path.read_bytes(), max_output_size=plain_bytes + 1024)
    if len(plain) != plain_bytes or hashlib.sha256(plain).hexdigest() != h.hexdigest():
        abs_path.unlink(missing_ok=True)
        raise RuntimeError("zstd roundtrip verify failed; chunk deleted")

    rec = ChunkRecord(
        chunk_id=chunk_id,
        path=rel_path,
        from_ts=from_ts,
        until_ts=until_ts,
        from_ts_iso=iso_utc(from_ts),
        until_ts_iso=iso_utc(until_ts),
        plain_bytes=plain_bytes,
        zstd_bytes=zstd_bytes,
        line_count=line_count,
        sha256_plain=h.hexdigest(),
        session_id=session_id,
        agent=agent,
        source=source_label,
        source_path=str(source.resolve()),
        sealed_at=datetime.now(timezone.utc).isoformat(),
        zstd_level=zstd_level,
    )
    append_manifest(fs_dir, rec)
    return rec


def verify_chunk(fs_dir: Path, rec: ChunkRecord) -> bool:
    abs_path = Path(fs_dir) / rec.path
    if not abs_path.exists():
        return False
    dctx = zstd.ZstdDecompressor()
    plain = dctx.decompress(abs_path.read_bytes(), max_output_size=rec.plain_bytes + 1024)
    if len(plain) != rec.plain_bytes:
        return False
    return hashlib.sha256(plain).hexdigest() == rec.sha256_plain


def open_chunk_lines(fs_dir: Path, rec: ChunkRecord) -> Iterator[bytes]:
    abs_path = Path(fs_dir) / rec.path
    dctx = zstd.ZstdDecompressor()
    with open(abs_path, "rb") as f, dctx.stream_reader(f) as reader:
        buf = b""
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            buf += chunk
            while True:
                i = buf.find(b"\n")
                if i < 0:
                    break
                yield buf[: i + 1]
                buf = buf[i + 1 :]
        if buf:
            yield buf


def chunks_covering_ts(
    records: List[ChunkRecord], t0: float, t1: float
) -> List[ChunkRecord]:
    """Chunks whose [from_ts, until_ts] overlaps [t0, t1]."""
    out = []
    for r in records:
        if r.from_ts is None or r.until_ts is None:
            out.append(r)  # unknown — include
            continue
        if r.until_ts < t0 or r.from_ts > t1:
            continue
        out.append(r)
    return out


def rewrite_hot_tail(
    hot_path: Path,
    cut: int,
    *,
    apply: bool,
) -> Dict[str, Any]:
    """
    Replace hot_path with bytes [cut:]. If apply=False, only report plan.
    Uses temp file + os.replace. Caller must ensure writers are paused or accept race.
    """
    hot_path = Path(hot_path)
    size = hot_path.stat().st_size
    if cut <= 0:
        return {"action": "noop", "reason": "cut<=0", "size": size}
    if cut >= size:
        return {"action": "noop", "reason": "cut>=size", "size": size}
    tail_size = size - cut
    plan = {
        "action": "rewrite_tail",
        "cut": cut,
        "size_before": size,
        "tail_bytes": tail_size,
        "apply": apply,
    }
    if not apply:
        return plan

    fd, tmp = tempfile.mkstemp(dir=str(hot_path.parent), suffix=".updates.tail")
    try:
        with os.fdopen(fd, "wb") as out_f, open(hot_path, "rb") as in_f:
            in_f.seek(cut)
            while True:
                buf = in_f.read(8 * 1024 * 1024)
                if not buf:
                    break
                out_f.write(buf)
            out_f.flush()
            os.fsync(out_f.fileno())
        os.replace(tmp, hot_path)
        dir_fd = os.open(str(hot_path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    plan["size_after"] = hot_path.stat().st_size
    return plan


def plan_prune_hot(
    hot_path: Path,
    *,
    max_bytes: int = HOT_MAX_BYTES,
    keep_bytes: int = HOT_KEEP_BYTES,
) -> Dict[str, Any]:
    hot_path = Path(hot_path)
    size = hot_path.stat().st_size
    if size < max_bytes:
        return {
            "needed": False,
            "size": size,
            "max_bytes": max_bytes,
            "keep_bytes": keep_bytes,
            "reason": f"under hot max ({size} < {max_bytes})",
        }
    cut = find_prune_cut(hot_path, keep_bytes=keep_bytes)
    if cut <= 0:
        return {"needed": False, "size": size, "reason": "cut=0"}
    return {
        "needed": True,
        "size": size,
        "max_bytes": max_bytes,
        "keep_bytes": keep_bytes,
        "cut": cut,
        "prefix_bytes": cut,
        "tail_bytes": size - cut,
    }


def prune_hot(
    hot_path: Path,
    fs_dir: Path,
    *,
    apply: bool = False,
    agent: Optional[str] = None,
    session_id: Optional[str] = None,
    max_bytes: int = HOT_MAX_BYTES,
    keep_bytes: int = HOT_KEEP_BYTES,
    zstd_level: int = DEFAULT_ZSTD_LEVEL,
) -> Dict[str, Any]:
    """
    If hot >= max_bytes: seal [0:cut) into V2, verify, then if apply rewrite tail.
    Default apply=False (dry-run).

    SAFETY: refuses backend-native paths (.grok/sessions, .codex/sessions, .claude/projects).
    Target must be AA history/hot.jsonl (or legacy full_stream/hot.jsonl).
    """
    hot_path = Path(hot_path)
    # inline guard (avoid circular import with aa_stream)
    s = str(hot_path.resolve())
    for m in ("/.grok/sessions/", "/.codex/sessions/", "/.claude/projects/", "/.claude/sessions/"):
        if m in s:
            raise ValueError(f"refusing to prune backend-native path: {hot_path}")
    if hot_path.name != HOT_NAME and apply:
        raise ValueError(f"apply prune only on AA {HOT_NAME}, got {hot_path.name}")
    plan = plan_prune_hot(hot_path, max_bytes=max_bytes, keep_bytes=keep_bytes)
    if not plan.get("needed"):
        return {"status": "skip", **plan}

    cut = plan["cut"]
    result: Dict[str, Any] = {"status": "planned", "plan": plan, "apply": apply}

    # Always seal when prune is needed (even dry-run can seal with --seal-only;
    # here dry-run does not write; apply seals then rewrites).
    if not apply:
        result["status"] = "dry_run"
        result["would_seal_bytes"] = cut
        result["would_keep_bytes"] = plan["tail_bytes"]
        return result

    rec = seal_byte_range(
        Path(hot_path),
        Path(fs_dir),
        start=0,
        end=cut,
        agent=agent,
        session_id=session_id,
        source_label="prune_hot",
        zstd_level=zstd_level,
    )
    if not verify_chunk(fs_dir, rec):
        raise RuntimeError("post-seal verify failed; refusing to prune hot")
    tail = rewrite_hot_tail(Path(hot_path), cut, apply=True)
    result["status"] = "pruned"
    result["chunk"] = json.loads(rec.to_json())
    result["tail"] = tail
    return result


def resolve_agent_home(agent: str, agents_json: Optional[Path] = None) -> Path:
    """Best-effort agent home from agents.json or ~/agents/<Name>."""
    candidates = []
    if agents_json is None:
        for p in (
            Path.home() / "agents" / "config" / "agents.json",  # machine-canonical roster
            Path.home() / "projects" / "agent-abide" / "agents.json",
            Path.home() / "projects" / "agent-abide-dev" / "agents.json",
            Path.home() / "agents" / "agents.json",
        ):
            if p.exists():
                agents_json = p
                break
    if agents_json and agents_json.exists():
        data = json.loads(agents_json.read_text())
        # formats vary
        agents = data.get("agents") or data
        if isinstance(agents, dict):
            ent = agents.get(agent) or agents.get(agent.lower())
            if isinstance(ent, str):
                candidates.append(Path(ent))
            elif isinstance(ent, dict):
                for k in ("home", "cwd", "path"):
                    if ent.get(k):
                        candidates.append(Path(ent[k]))
        elif isinstance(agents, list):
            for ent in agents:
                if isinstance(ent, dict) and ent.get("name") == agent:
                    for k in ("home", "cwd", "path"):
                        if ent.get(k):
                            candidates.append(Path(ent[k]))
    candidates.append(Path.home() / "agents" / agent)
    # Nested LeviSmith homes (Squiggy, Wend, Lenny, …)
    candidates.append(Path.home() / "agents" / "LeviSmith" / agent)
    # Prefer first existing candidate that has asdaaas/. If several, prefer the one
    # with conversation.jsonl or history/ (real spine) over empty stubs.
    found = []
    for c in candidates:
        if c.is_dir() and (c / "asdaaas").is_dir():
            found.append(c.resolve())
    if not found:
        raise FileNotFoundError(f"agent home not found for {agent!r}; tried {candidates}")
    def score(p: Path) -> tuple:
        aa = p / "asdaaas"
        return (
            1 if (aa / "conversation.jsonl").is_file() else 0,
            1 if (aa / "history").is_dir() or (aa / "full_stream").is_dir() else 0,
            1 if (aa / "health.json").is_file() else 0,
            # longer path slightly preferred when scores tie (nested over stub)
            len(str(p)),
        )
    found.sort(key=score, reverse=True)
    return found[0]


def find_live_updates(agent_home: Path, session_id: Optional[str] = None) -> Optional[Path]:
    """Locate live updates.jsonl under ~/.grok/sessions for this agent cwd."""
    # encode path like grok does: URL-encode /
    home = Path(agent_home).resolve()
    # try common encodings
    from urllib.parse import quote

    key = quote(str(home), safe="")
    root = Path.home() / ".grok" / "sessions" / key
    if not root.is_dir():
        # try raw
        alt = Path.home() / ".grok" / "sessions" / str(home).replace("/", "%2F")
        root = alt if alt.is_dir() else root
    if not root.is_dir():
        return None
    if session_id:
        p = root / session_id / "updates.jsonl"
        return p if p.exists() else None
    # largest updates.jsonl
    best = None
    best_sz = -1
    for u in root.rglob("updates.jsonl"):
        if "backup_" in str(u):
            continue
        sz = u.stat().st_size
        if sz > best_sz:
            best_sz = sz
            best = u
    return best
