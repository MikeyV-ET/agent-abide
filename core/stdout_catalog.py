"""Y-channel stdout wire recorder: catalog of atomic exemplars + timeline refs.

Problem
-------
The old stdout_log.jsonl wrote every full JSON-RPC line with a timestamp.
Long-lived agents produced 100MB–GB files because streaming session/update
chunks are nearly unique by content but not by *kind*.

Design (Eric 2026-08-28)
------------------------
1. **Catalog** (`stdout_catalog.jsonl`): each distinct *thing we've seen*
   once, with an integer id and a full exemplar payload (first sighting).
2. **Timeline** (`stdout_log.jsonl`): every wire event as
   ``{"ts": <float>, "ref": <id>}`` — when it showed up + pointer.

Keying
------
- Exact content hash for frames we need full forensic uniqueness on
  (gates, unknown methods, non-JSON).
- **Type-level** key for high-churn ``session/update`` payloads whose
  bodies already land in ``updates.jsonl`` (speech/tool chunks). First
  full example is kept; later arrivals only bump the timeline.

Restart-safe: on open, reload catalog into memory so ids stay stable
across asdaaas restarts on the same session dir.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import IO, Optional


# sessionUpdate kinds whose full body is already in updates.jsonl —
# catalog stores one atomic exemplar per kind, not every chunk.
_HIGH_CHURN_SESSION_UPDATES = frozenset({
    "agent_message_chunk",
    "agent_thought_chunk",
    "user_message_chunk",
    "tool_call",
    "tool_call_update",
    "turn_completed",
    "plan",
})


def catalog_key(raw: str) -> str:
    """Stable key for 'have we seen this kind of wire frame before?'"""
    try:
        frame = json.loads(raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return "raw:" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()

    if not isinstance(frame, dict):
        return "raw:" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()

    method = frame.get("method") or ""
    if method == "session/update":
        params = frame.get("params") or {}
        update = params.get("update") if isinstance(params, dict) else None
        if isinstance(update, dict):
            su = update.get("sessionUpdate") or ""
            if su in _HIGH_CHURN_SESSION_UPDATES:
                return f"su:{su}"
            # Other session updates (compact, commands list, …): key by type
            # plus a content hash so genuinely new shapes still get exemplars.
            body = json.dumps(update, sort_keys=True, separators=(",", ":"))
            h = hashlib.sha256(body.encode()).hexdigest()[:16]
            return f"su:{su}:{h}" if su else f"su:?:{h}"

    if method:
        # Same method + same params body → one exemplar (repeat notifications).
        # Drop jsonrpc id so repeated request shapes collapse.
        params = frame.get("params", frame.get("result", frame.get("error")))
        try:
            body = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            body = str(params)
        h = hashlib.sha256(body.encode()).hexdigest()
        return f"m:{method}:{h}"

    # Responses / odd frames: full raw hash
    return "raw:" + hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


class StdoutWireRecorder:
    """Append-only catalog + timeline. Safe no-op if paths cannot be opened."""

    def __init__(self, session_dir: Path):
        self._session_dir = Path(session_dir)
        self._catalog_path = self._session_dir / "stdout_catalog.jsonl"
        self._timeline_path = self._session_dir / "stdout_log.jsonl"
        self._key_to_id: dict[str, int] = {}
        self._next_id = 1
        self._catalog_fp: Optional[IO[str]] = None
        self._timeline_fp: Optional[IO[str]] = None
        self._open()

    def _open(self) -> None:
        try:
            self._session_dir.mkdir(parents=True, exist_ok=True)
            self._reload_catalog()
            self._catalog_fp = open(self._catalog_path, "a", encoding="utf-8")
            self._timeline_fp = open(self._timeline_path, "a", encoding="utf-8")
        except OSError:
            self.close()

    def _reload_catalog(self) -> None:
        """Load existing catalog so restarts keep stable ids."""
        if not self._catalog_path.exists():
            return
        try:
            with open(self._catalog_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kid = rec.get("id")
                    key = rec.get("key")
                    if isinstance(kid, int) and isinstance(key, str):
                        self._key_to_id[key] = kid
                        if kid >= self._next_id:
                            self._next_id = kid + 1
        except OSError:
            pass

    @property
    def active(self) -> bool:
        return self._timeline_fp is not None and self._catalog_fp is not None

    def record(self, ts: float, raw: str) -> Optional[int]:
        """Record one wire line. Returns catalog id, or None if inactive."""
        if not self.active:
            return None
        key = catalog_key(raw)
        cid = self._key_to_id.get(key)
        is_new = cid is None
        if is_new:
            cid = self._next_id
            self._next_id += 1
            self._key_to_id[key] = cid
            cat = {
                "id": cid,
                "key": key,
                "example": raw,
            }
            try:
                self._catalog_fp.write(json.dumps(cat, ensure_ascii=False) + "\n")
                self._catalog_fp.flush()
            except Exception:
                # Roll back in-memory so a later retry can re-add
                self._key_to_id.pop(key, None)
                return None
        try:
            self._timeline_fp.write(
                json.dumps({"ts": ts, "ref": cid}, ensure_ascii=False) + "\n"
            )
            self._timeline_fp.flush()
        except Exception:
            pass
        return cid

    def close(self) -> None:
        for fp in (self._catalog_fp, self._timeline_fp):
            if fp is not None:
                try:
                    fp.close()
                except Exception:
                    pass
        self._catalog_fp = None
        self._timeline_fp = None
