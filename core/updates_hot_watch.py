"""Native session file → hot.jsonl: inotify wake + debounced sync poller.

aa-dev only. Complements frame-driven ``sync_hot_stream()`` calls so hot
stays caught up when the turn loop is quiet/delayed (Squiggy mid-tool gap).

Pattern (Eric): inotify is the doorbell; debounced poller opens the door
and runs checkpointed ``tail_*_once`` / ``sync_hot_stream``.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import logging
import os
import select
import struct
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("asdaaas.updates_hot_watch")

_IN_MODIFY = 0x00000002
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVE_SELF = 0x00000800
_IN_DELETE_SELF = 0x00000400
_IN_CREATE = 0x00000100  # if watching dir for file create

# Default debounce: coalesce burst appends mid-tool
DEFAULT_DEBOUNCE_S = 0.05
# Safety poll even without events (missed watch / replace)
DEFAULT_IDLE_POLL_S = 1.0


class UpdatesHotWatcher:
    """Watch one native updates/session file; debounced sync callback."""

    def __init__(
        self,
        path: Path | str,
        sync_fn: Callable[[], object],
        *,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        idle_poll_s: float = DEFAULT_IDLE_POLL_S,
        name: str = "updates-hot",
    ):
        self.path = Path(path)
        self.sync_fn = sync_fn
        self.debounce_s = max(0.05, float(debounce_s))
        self.idle_poll_s = max(0.5, float(idle_poll_s))
        self.name = name
        self._stop = threading.Event()
        self._dirty = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sync_lock = threading.Lock()
        self._fd: Optional[int] = None
        self._wd: Optional[int] = None
        self._watch_dir = False  # watching parent for create
        self._libc = None
        self._poll_only = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"updates-hot-{self.name}",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "UpdatesHotWatcher start name=%s path=%s debounce=%.2fs",
            self.name,
            self.path,
            self.debounce_s,
        )

    def stop(self) -> None:
        self._stop.set()
        self._dirty.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        self._teardown_inotify()
        self._thread = None

    def kick(self) -> None:
        """External wake (e.g. after known frame write)."""
        self._dirty.set()

    def _run_sync(self) -> None:
        if not self._sync_lock.acquire(blocking=False):
            # already syncing; leave dirty for another pass
            self._dirty.set()
            return
        try:
            try:
                r = self.sync_fn()
                if isinstance(r, dict) and r.get("status") not in (
                    None,
                    "ok",
                    "skipped",
                    "noop",
                    "caught_up",
                ):
                    log.debug("sync_fn status=%s", r.get("status") or r)
            except Exception as e:
                log.warning("sync_fn error name=%s: %s", self.name, e)
        finally:
            self._sync_lock.release()

    def _init_inotify(self) -> bool:
        try:
            self._libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            self._libc.inotify_init.restype = ctypes.c_int
            self._libc.inotify_add_watch.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint32,
            ]
            self._libc.inotify_add_watch.restype = ctypes.c_int
            self._libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
            self._libc.inotify_rm_watch.restype = ctypes.c_int
            fd = self._libc.inotify_init()
            if fd < 0:
                log.warning("inotify_init failed — poll-only name=%s", self.name)
                self._poll_only = True
                return False
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            self._fd = fd
            return self._add_watch()
        except Exception as e:
            log.warning("inotify init failed name=%s: %s — poll-only", self.name, e)
            self._poll_only = True
            self._fd = None
            return False

    def _add_watch(self) -> bool:
        if self._fd is None or self._libc is None:
            return False
        # Prefer watching the file; if missing, watch parent for CREATE
        mask = _IN_MODIFY | _IN_CLOSE_WRITE | _IN_MOVE_SELF | _IN_DELETE_SELF
        target = self.path
        self._watch_dir = False
        if not target.exists():
            target = self.path.parent
            mask = _IN_CREATE | _IN_MODIFY | _IN_CLOSE_WRITE | _IN_MOVED_TO if False else (_IN_CREATE | _IN_MODIFY | _IN_CLOSE_WRITE)
            # IN_MOVED_TO = 0x00000080
            mask = _IN_CREATE | _IN_MODIFY | _IN_CLOSE_WRITE | 0x00000080
            self._watch_dir = True
            if not target.exists():
                try:
                    target.mkdir(parents=True, exist_ok=True)
                except Exception:
                    self._poll_only = True
                    return False
        wd = self._libc.inotify_add_watch(self._fd, str(target).encode(), mask)
        if wd < 0:
            log.warning(
                "inotify_add_watch failed name=%s path=%s errno=%s — poll-only",
                self.name,
                target,
                ctypes.get_errno(),
            )
            self._poll_only = True
            return False
        self._wd = wd
        log.info(
            "UpdatesHotWatcher watch name=%s target=%s wd=%s dir=%s",
            self.name,
            target,
            wd,
            self._watch_dir,
        )
        return True

    def _teardown_inotify(self) -> None:
        if self._fd is not None and self._wd is not None and self._libc is not None:
            try:
                self._libc.inotify_rm_watch(self._fd, self._wd)
            except Exception:
                pass
        if self._fd is not None:
            try:
                os.close(self._fd)
            except Exception:
                pass
        self._fd = None
        self._wd = None

    def _rearm_watch(self) -> None:
        self._teardown_inotify()
        self._poll_only = False
        self._init_inotify()

    def _drain_inotify(self) -> bool:
        """Read pending events. Returns True if we should treat as dirty."""
        if self._fd is None:
            return False
        dirty = False
        need_rearm = False
        try:
            while True:
                try:
                    buf = os.read(self._fd, 4096)
                except BlockingIOError:
                    break
                if not buf:
                    break
                dirty = True
                # parse events for DELETE_SELF / MOVE_SELF
                off = 0
                while off + 16 <= len(buf):
                    wd, mask, _cookie, name_len = struct.unpack_from("iIII", buf, off)
                    off += 16 + name_len
                    if mask & (_IN_MOVE_SELF | _IN_DELETE_SELF):
                        need_rearm = True
                    if self._watch_dir and name_len:
                        # name is after header
                        pass
        except Exception as e:
            log.debug("drain_inotify: %s", e)
            need_rearm = True
        if need_rearm:
            self._rearm_watch()
            # If file now exists as file watch, good
            if self.path.exists() and self._watch_dir:
                self._rearm_watch()
            dirty = True
        return dirty

    def _run(self) -> None:
        self._init_inotify()
        # Initial catch-up
        self._run_sync()
        while not self._stop.is_set():
            if self._poll_only or self._fd is None:
                # Slow poll fallback — also honor kick()/dirty
                if self._dirty.wait(self.idle_poll_s):
                    if self._stop.is_set():
                        break
                    self._dirty.clear()
                    time.sleep(self.debounce_s)
                    self._run_sync()
                    continue
                if self._stop.is_set():
                    break
                self._run_sync()
                continue

            # Wait for inotify or idle poll timeout
            try:
                r, _, _ = select.select([self._fd], [], [], self.idle_poll_s)
            except Exception:
                r = []
            if self._stop.is_set():
                break
            if r:
                if self._drain_inotify():
                    self._dirty.set()
            else:
                # idle timeout — safety sync
                self._dirty.set()

            if not self._dirty.is_set():
                continue
            # Debounce: wait a bit for burst writes
            self._dirty.clear()
            time.sleep(self.debounce_s)
            # absorb further events during debounce
            if self._fd is not None:
                self._drain_inotify()
                self._dirty.clear()
            self._run_sync()


def start_updates_hot_watcher(
    backend,
    *,
    debounce_s: float = DEFAULT_DEBOUNCE_S,
) -> Optional[UpdatesHotWatcher]:
    """Start watcher for backend's native session file if hot ingest is on.

    Returns watcher or None if not applicable.
    """
    if not getattr(backend, "_hot_ingest", False):
        return None
    # Resolve path
    path = None
    session_dir = getattr(backend, "_session_dir", None)
    if session_dir:
        cand = Path(session_dir) / "updates.jsonl"
        path = cand
    # Claude: session jsonl path if exposed
    if path is None:
        claude_path = getattr(backend, "_session_path", None) or getattr(
            backend, "_claude_session_path", None
        )
        if claude_path:
            path = Path(claude_path)
    if path is None:
        # Try find from agent home meta later — still start with kick-only
        agent_home = getattr(backend, "_agent_home", None)
        agent_name = getattr(backend, "_agent_name", None)
        if not agent_home or not agent_name:
            return None
        # Grok live finder
        try:
            from aa_stream import find_live_updates
            found = find_live_updates(Path(agent_home), agent_name)
            if found:
                path = Path(found)
        except Exception:
            pass
    if path is None:
        log.info(
            "updates_hot_watch: no native path yet for %s — skip watcher",
            getattr(backend, "_agent_name", "?"),
        )
        return None

    def _sync():
        return backend.sync_hot_stream()

    w = UpdatesHotWatcher(
        path,
        _sync,
        debounce_s=debounce_s,
        name=str(getattr(backend, "_agent_name", path.name)),
    )
    # Store on backend for stop/kick
    backend._updates_hot_watcher = w
    w.start()
    return w
