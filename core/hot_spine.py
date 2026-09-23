"""Hot spine: native backend log → hot.jsonl (AA unification / memory tape).

Mental model
------------
Native flight recorder (Grok updates.jsonl, Claude session jsonl, …) is the
binary's mouth. **hot.jsonl** is the AA-owned unified live tape: glass,
memory packing, and cross-backend consumers read AA shape — not native shape.

This module is the **always-on ear + projector** for that tape:

* One arming path per backend session (not a hitchhiker on turn collect).
* Live path: inotify doorbell + debounced checkpointed tail (updates_hot_watch).
* Reconcile path: periodic / on-demand ``behind = native_size - checkpoint``;
  if behind > 0, same ``sync_fn`` catch-up (no second writer law).

Turn collect (FileEventSource) stays a separate reader for delivery until a
later phase puts collect on a shared batch bus. Delivery bugs must not be the
only reason the spine stays current — that is why HotSpine outlives a turn.

Phase 1 (this file): spine ownership + reconcile API + session-ready start.
Phase 2 (later): single NativeTail fan-out to collect + occupancy + hot.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("asdaaas.hot_spine")

# Reconcile cadence when idle (in addition to watcher's idle_poll)
DEFAULT_RECONCILE_S = 2.0


@dataclass
class ReconcileStatus:
    """Result of comparing hot checkpoint to native EOF."""

    ok: bool
    backend: str
    native_path: Optional[str]
    native_size: int
    checkpoint_offset: int
    behind: int
    caught_up: bool = False
    sync_result: Optional[dict] = None
    error: Optional[str] = None

    def as_dict(self) -> dict:
        d = {
            "ok": self.ok,
            "backend": self.backend,
            "native_path": self.native_path,
            "native_size": self.native_size,
            "checkpoint_offset": self.checkpoint_offset,
            "behind": self.behind,
            "caught_up": self.caught_up,
        }
        if self.sync_result is not None:
            d["sync_result"] = self.sync_result
        if self.error:
            d["error"] = self.error
        return d


def _read_grok_checkpoint(agent_home: Path, agent: str) -> tuple[Optional[Path], int]:
    """Return (source_path_or_None, byte_offset) from hot.meta / sources."""
    try:
        from aa_stream import resolve_history_dir, read_hot_meta, read_checkpoint
    except Exception:
        return None, 0
    fs = resolve_history_dir(Path(agent_home))
    meta = read_hot_meta(fs) or {}
    backends = meta.get("backends") or {}
    g = backends.get("grok") or {}
    path_s = g.get("source_path")
    off = int(g.get("byte_offset") or 0)
    if path_s:
        return Path(path_s), off
    try:
        ck = read_checkpoint(fs, "grok")
        if ck.get("path"):
            return Path(ck["path"]), int(ck.get("byte_offset") or 0)
    except Exception:
        pass
    return None, off


def measure_behind(
    *,
    agent_home: Path,
    agent: str,
    native_path: Optional[Path] = None,
    backend: str = "grok",
) -> ReconcileStatus:
    """Compare hot checkpoint to native file size (no write)."""
    if backend != "grok":
        return ReconcileStatus(
            ok=False,
            backend=backend,
            native_path=str(native_path) if native_path else None,
            native_size=0,
            checkpoint_offset=0,
            behind=0,
            error=f"measure_behind: backend {backend!r} not implemented yet",
        )
    path, off = _read_grok_checkpoint(agent_home, agent)
    if native_path is not None:
        path = Path(native_path)
    if path is None or not path.exists():
        return ReconcileStatus(
            ok=False,
            backend=backend,
            native_path=str(path) if path else None,
            native_size=0,
            checkpoint_offset=off,
            behind=0,
            error="native path missing",
        )
    try:
        size = path.stat().st_size
    except OSError as e:
        return ReconcileStatus(
            ok=False,
            backend=backend,
            native_path=str(path),
            native_size=0,
            checkpoint_offset=off,
            behind=0,
            error=str(e),
        )
    # Truncation: treat as need full resync signal (behind = size, caller resets)
    if off > size:
        behind = size  # force catch-up after reset inside tail_grok_once
    else:
        behind = max(0, size - off)
    return ReconcileStatus(
        ok=True,
        backend=backend,
        native_path=str(path),
        native_size=size,
        checkpoint_offset=off,
        behind=behind,
    )


class HotSpine:
    """Always-on native→hot projector + reconcile for one backend session.

    Owns the UpdatesHotWatcher lifecycle. Backend calls start() after
    session_dir is known; stop() on shutdown.
    """

    def __init__(
        self,
        *,
        agent_home: Path | str,
        agent_name: str,
        backend_name: str = "grok",
        sync_fn: Callable[[], dict],
        native_path: Optional[Path | str] = None,
        reconcile_s: float = DEFAULT_RECONCILE_S,
    ):
        self.agent_home = Path(agent_home)
        self.agent_name = agent_name
        self.backend_name = backend_name
        self.sync_fn = sync_fn
        self.native_path = Path(native_path) if native_path else None
        self.reconcile_s = max(0.5, float(reconcile_s))
        self._watcher = None
        self._reconcile_stop = threading.Event()
        self._reconcile_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_reconcile: Optional[dict] = None

    def start(self) -> "HotSpine":
        """Arm watcher + reconcile loop. Idempotent."""
        with self._lock:
            self._start_watcher_unlocked()
            self._start_reconcile_unlocked()
        # Immediate catch-up
        try:
            self.sync_fn()
        except Exception as e:
            log.warning("HotSpine initial sync: %s", e)
        st = self.reconcile(catch_up=True)
        log.info(
            "HotSpine start agent=%s backend=%s path=%s behind=%s",
            self.agent_name,
            self.backend_name,
            self.native_path or st.native_path,
            st.behind,
        )
        return self

    def stop(self) -> None:
        with self._lock:
            self._reconcile_stop.set()
            t = self._reconcile_thread
            w = self._watcher
            self._reconcile_thread = None
            self._watcher = None
        if t and t.is_alive():
            t.join(timeout=2.0)
        if w is not None:
            try:
                w.stop()
            except Exception:
                pass

    def kick(self) -> None:
        w = self._watcher
        if w is not None:
            try:
                w.kick()
            except Exception:
                pass

    def reconcile(self, *, catch_up: bool = True) -> ReconcileStatus:
        """Measure behind; optionally run sync_fn until caught or one pass.

        Uses the same projector as live (sync_fn → tail_*_once). One append law.
        """
        st = measure_behind(
            agent_home=self.agent_home,
            agent=self.agent_name,
            native_path=self.native_path,
            backend=self.backend_name,
        )
        if not st.ok:
            self._last_reconcile = st.as_dict()
            return st
        if st.behind <= 0 or not catch_up:
            self._last_reconcile = st.as_dict()
            return st
        # Catch-up pass(es): tail may cap max_lines; loop while still behind
        last_sync = None
        for _ in range(32):
            try:
                last_sync = self.sync_fn()
            except Exception as e:
                st.error = str(e)
                st.sync_result = last_sync if isinstance(last_sync, dict) else None
                self._last_reconcile = st.as_dict()
                return st
            st2 = measure_behind(
                agent_home=self.agent_home,
                agent=self.agent_name,
                native_path=self.native_path,
                backend=self.backend_name,
            )
            st = st2
            st.sync_result = last_sync if isinstance(last_sync, dict) else None
            if st.behind <= 0:
                st.caught_up = True
                break
            # No progress → stop
            if isinstance(last_sync, dict) and int(last_sync.get("lines_ingested") or 0) == 0:
                break
        self._last_reconcile = st.as_dict()
        if st.behind > 0:
            log.warning(
                "HotSpine reconcile still behind=%s agent=%s",
                st.behind,
                self.agent_name,
            )
        return st

    def status(self) -> dict:
        st = measure_behind(
            agent_home=self.agent_home,
            agent=self.agent_name,
            native_path=self.native_path,
            backend=self.backend_name,
        )
        return {
            "agent": self.agent_name,
            "backend": self.backend_name,
            "watcher_alive": bool(
                self._watcher
                and getattr(self._watcher, "_thread", None)
                and self._watcher._thread.is_alive()
            ),
            "reconcile_alive": bool(
                self._reconcile_thread and self._reconcile_thread.is_alive()
            ),
            "last_reconcile": self._last_reconcile,
            **st.as_dict(),
        }

    def _start_watcher_unlocked(self) -> None:
        if self._watcher is not None:
            try:
                self._watcher.stop()
            except Exception:
                pass
            self._watcher = None
        path = self.native_path
        if path is None:
            log.info(
                "HotSpine: no native_path yet agent=%s — reconcile-only until path set",
                self.agent_name,
            )
            return
        from updates_hot_watch import UpdatesHotWatcher

        w = UpdatesHotWatcher(
            path,
            self.sync_fn,
            name=f"spine-{self.agent_name}",
        )
        self._watcher = w
        w.start()

    def _start_reconcile_unlocked(self) -> None:
        if self._reconcile_thread and self._reconcile_thread.is_alive():
            return
        self._reconcile_stop.clear()

        def _loop():
            while not self._reconcile_stop.wait(self.reconcile_s):
                try:
                    st = self.reconcile(catch_up=True)
                    if st.behind > 0 and st.caught_up is False:
                        self.kick()
                except Exception as e:
                    log.warning("HotSpine reconcile loop: %s", e)

        self._reconcile_thread = threading.Thread(
            target=_loop,
            name=f"hot-spine-reconcile-{self.agent_name}",
            daemon=True,
        )
        self._reconcile_thread.start()

    def set_native_path(self, path: Path | str) -> None:
        """Update native path and re-arm watcher (e.g. session became ready)."""
        with self._lock:
            self.native_path = Path(path)
            self._start_watcher_unlocked()


def start_hot_spine_for_backend(backend) -> Optional[HotSpine]:
    """Arm HotSpine on a backend that has configure_aa_history state.

    Call after ``_session_dir`` is set so native path is known.
    """
    if not getattr(backend, "_hot_ingest", False):
        return None
    home = getattr(backend, "_agent_home", None)
    name = getattr(backend, "_agent_name", None)
    if not home or not name:
        return None

    # stop prior spine / watcher
    old = getattr(backend, "_hot_spine", None)
    if old is not None:
        try:
            old.stop()
        except Exception:
            pass
    old_w = getattr(backend, "_updates_hot_watcher", None)
    if old_w is not None:
        try:
            old_w.stop()
        except Exception:
            pass
        backend._updates_hot_watcher = None

    native = None
    session_dir = getattr(backend, "_session_dir", None)
    if session_dir:
        cand = Path(session_dir) / "updates.jsonl"
        native = cand
    if native is None:
        try:
            from aa_stream import find_live_updates
            found = find_live_updates(Path(home), name)
            if found:
                native = Path(found)
        except Exception:
            pass

    def _sync():
        return backend.sync_hot_stream()

    spine = HotSpine(
        agent_home=home,
        agent_name=name,
        backend_name="grok",
        sync_fn=_sync,
        native_path=native,
    )
    backend._hot_spine = spine
    # Keep _updates_hot_watcher alias for kick sites that still use it
    spine.start()
    if spine._watcher is not None:
        backend._updates_hot_watcher = spine._watcher
    print(
        f"[hot_spine] armed agent={name} native={native} "
        f"watcher={spine.status().get('watcher_alive')} "
        f"reconcile={spine.status().get('reconcile_alive')}"
    )
    return spine
