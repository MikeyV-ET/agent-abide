"""Grok native bus: one ear on updates.jsonl (+ events.jsonl), fan-out.

Phase 2 of hot spine architecture:

* **One cursor** on ``updates.jsonl`` (JsonlByteTail).
* Each pump: parse complete lines → hot projector (ingest_grok_records) and
  optional collect buffer (FileEventSource window).
* ``events.jsonl`` has its own cursor (lifecycle only; not the AA tape).

Occupancy (binary_state) stays on its own tail until phase 3.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from native_tail import JsonlByteTail, LineBatch

log = logging.getLogger("asdaaas.grok_native_bus")


class GrokNativeBus:
    """Shared native reader for one Grok session directory."""

    def __init__(
        self,
        session_dir: Path | str,
        *,
        agent_home: Path | str,
        agent_name: str,
        session_id: Optional[str] = None,
        start_offset: int = 0,
    ):
        self.session_dir = Path(session_dir)
        self.agent_home = Path(agent_home)
        self.agent_name = agent_name
        self.session_id = session_id or self.session_dir.name
        self.updates_path = self.session_dir / "updates.jsonl"
        self.events_path = self.session_dir / "events.jsonl"
        self.updates = JsonlByteTail(self.updates_path)
        self.events = JsonlByteTail(self.events_path)
        self.updates.seek(start_offset)
        # events: collect wants EOF-relative; start at end after open_collect
        self._lock = threading.RLock()
        self._collect_active = False
        self._collect_updates: List[dict] = []
        self._collect_events: List[dict] = []
        self._on_hot: Optional[Callable[[dict], None]] = None
        self._last_pump: Optional[dict] = None

    def set_hot_hook(self, fn: Optional[Callable[[dict], None]]) -> None:
        self._on_hot = fn

    def close(self) -> None:
        self.updates.close()
        self.events.close()

    def begin_collect_window(self) -> None:
        """Mark collect attachment at current tips (like FileEventSource.seek EOF).

        Does **not** move the updates cursor backward or to EOF if behind —
        hot still owns catch-up via pump. Collect only receives lines at/after
        the current updates.offset and events tip.
        """
        with self._lock:
            # Events: collect should not see old lifecycle — jump events to EOF
            if self.events_path.exists():
                self.events.seek_end()
            else:
                self.events.seek(0)
            self._collect_updates.clear()
            self._collect_events.clear()
            self._collect_active = True
            log.debug(
                "collect window begin updates_off=%s events_off=%s",
                self.updates.offset,
                self.events.offset,
            )

    def end_collect_window(self) -> None:
        with self._lock:
            self._collect_active = False
            self._collect_updates.clear()
            self._collect_events.clear()

    def pump(self, *, max_lines: Optional[int] = None) -> dict:
        """Read new complete lines; project hot; buffer for collect."""
        with self._lock:
            return self._pump_unlocked(max_lines=max_lines)

    def _pump_unlocked(self, *, max_lines: Optional[int] = None) -> dict:
        u_batch = self.updates.read_batch(max_lines=max_lines)
        e_batch = self.events.read_batch(max_lines=max_lines)
        parsed: List[Tuple[int, dict]] = []
        for rec in u_batch.records:
            if not rec.text.strip():
                continue
            try:
                obj = json.loads(rec.text)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                parsed.append((rec.offset, obj))
                if self._collect_active:
                    self._collect_updates.append(obj)

        for rec in e_batch.records:
            if not rec.text.strip():
                continue
            try:
                obj = json.loads(rec.text)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and self._collect_active:
                self._collect_events.append(obj)

        hot_result = None
        if u_batch.end_offset != u_batch.start_offset or parsed:
            try:
                from stream_adapters.grok import ingest_grok_records

                hot_result = ingest_grok_records(
                    self.agent_home,
                    self.agent_name,
                    session_id=self.session_id,
                    source=self.updates_path,
                    records=parsed,
                    end_offset=u_batch.end_offset,
                )
                if self._on_hot and isinstance(hot_result, dict):
                    try:
                        self._on_hot(hot_result)
                    except Exception:
                        pass
            except Exception as e:
                log.warning("hot ingest from bus: %s", e)
                hot_result = {"status": "error", "error": str(e)}

        out = {
            "status": "ok",
            "updates_lines": len(u_batch.records),
            "events_lines": len(e_batch.records),
            "parsed": len(parsed),
            "updates_offset": self.updates.offset,
            "events_offset": self.events.offset,
            "behind": self.updates.behind(),
            "hot": hot_result,
        }
        self._last_pump = out
        return out

    def read_for_collect(self) -> Tuple[List[dict], List[dict]]:
        """Pump then drain collect buffers (FileEventSource API)."""
        with self._lock:
            self._pump_unlocked()
            u = list(self._collect_updates)
            e = list(self._collect_events)
            self._collect_updates.clear()
            self._collect_events.clear()
            return u, e

    def catch_up(self, *, max_passes: int = 64) -> dict:
        """Pump until updates behind==0 or no progress."""
        last = {}
        for _ in range(max_passes):
            last = self.pump()
            if self.updates.behind() <= 0:
                break
            if int(last.get("updates_lines") or 0) == 0:
                break
        return last

    def status(self) -> dict:
        return {
            "updates_path": str(self.updates_path),
            "updates_offset": self.updates.offset,
            "updates_behind": self.updates.behind(),
            "events_offset": self.events.offset,
            "collect_active": self._collect_active,
            "collect_buf_u": len(self._collect_updates),
            "last_pump": self._last_pump,
        }
