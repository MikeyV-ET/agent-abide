"""Shared inotify hub for TUI hot.jsonl tails (SA AaStreamLiveTailer pattern).

Design to not blow inotify limits (lesson from SA shared-doc watcher + fork bugs):

- **One inotify instance (fd) per TUI process**, not one per agent tab.
- **One watch per absolute hot.jsonl path** (multi-tab same agent = one watch).
- Watches are **files**, not recursive trees (N agents ≈ N watches, not N×dirs).
- On EMFILE / watch failure → mark path as poll-only; do not crash the TUI.
- File replace (MOVE_SELF/DELETE_SELF) → drop + re-add watch.

Kernel caps (typical): max_user_instances, max_user_watches — we only burn
1 instance + ~#open_agents watches.
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
from typing import Dict, Optional, Set

log = logging.getLogger("tui.hot_inotify")

_IN_MODIFY = 0x00000002
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVE_SELF = 0x00000800
_IN_DELETE_SELF = 0x00000400

_lock = threading.Lock()
_hub: Optional["HotInotifyHub"] = None


def get_hub() -> "HotInotifyHub":
    global _hub
    with _lock:
        if _hub is None:
            _hub = HotInotifyHub()
        return _hub


class HotInotifyHub:
    def __init__(self):
        self._fd: Optional[int] = None
        self._wd_to_path: Dict[int, str] = {}
        self._path_to_wd: Dict[str, int] = {}
        self._poll_only: Set[str] = set()
        self._libc = None
        self._mu = threading.Lock()
        self._init_fd()

    def _init_fd(self) -> bool:
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
                errno = ctypes.get_errno()
                log.warning("inotify_init failed errno=%s — all paths poll", errno)
                self._fd = None
                return False
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            self._fd = fd
            log.info("HotInotifyHub: armed fd=%s (shared)", fd)
            return True
        except Exception as e:
            log.warning("HotInotifyHub init failed: %s — poll fallback", e)
            self._fd = None
            return False

    def watch(self, path: str | Path) -> bool:
        """Ensure path is watched. Returns True if inotify active for path."""
        path = str(Path(path).resolve())
        with self._mu:
            if path in self._poll_only:
                return False
            if path in self._path_to_wd:
                return self._fd is not None
            if self._fd is None:
                self._poll_only.add(path)
                return False
            mask = _IN_MODIFY | _IN_CLOSE_WRITE | _IN_MOVE_SELF | _IN_DELETE_SELF
            wd = self._libc.inotify_add_watch(self._fd, path.encode(), mask)
            if wd < 0:
                errno = ctypes.get_errno()
                log.warning(
                    "inotify_add_watch failed path=%s errno=%s — poll this path",
                    path,
                    errno,
                )
                self._poll_only.add(path)
                return False
            self._wd_to_path[wd] = path
            self._path_to_wd[path] = wd
            log.info("HotInotifyHub: watch + %s wd=%s (n=%s)", path, wd, len(self._path_to_wd))
            return True

    def unwatch(self, path: str | Path) -> None:
        path = str(Path(path).resolve())
        with self._mu:
            self._poll_only.discard(path)
            wd = self._path_to_wd.pop(path, None)
            if wd is None or self._fd is None:
                return
            self._wd_to_path.pop(wd, None)
            try:
                self._libc.inotify_rm_watch(self._fd, wd)
            except Exception:
                pass

    def wait(self, path: str | Path, timeout_s: float = 1.0) -> bool:
        """Block until *this* path may have changed, or timeout.

        Returns True if an event for this path (or global re-arm) was seen.
        If path is poll-only or hub dead, sleeps timeout and returns False.
        """
        path = str(Path(path).resolve())
        if path in self._poll_only or self._fd is None:
            time.sleep(max(0.05, timeout_s))
            return False
        # Ensure watched
        if path not in self._path_to_wd:
            if not self.watch(path):
                time.sleep(max(0.05, timeout_s))
                return False
        try:
            r, _, _ = select.select([self._fd], [], [], max(0.0, timeout_s))
        except (ValueError, OSError):
            time.sleep(min(timeout_s, 0.1))
            return False
        if not r:
            return False
        hit = False
        try:
            while True:
                data = os.read(self._fd, 65536)
                if not data:
                    break
                i = 0
                while i + 16 <= len(data):
                    wd, mask, cookie, name_len = struct.unpack_from("iIII", data, i)
                    i += 16 + name_len
                    pth = self._wd_to_path.get(wd)
                    if mask & (_IN_DELETE_SELF | _IN_MOVE_SELF):
                        # file replaced — re-arm if we still care
                        if pth:
                            with self._mu:
                                self._path_to_wd.pop(pth, None)
                                self._wd_to_path.pop(wd, None)
                            self.watch(pth)
                            if pth == path:
                                hit = True
                    elif pth == path:
                        hit = True
                    elif pth is not None:
                        # other agent hot changed — ignore for this waiter
                        pass
        except BlockingIOError:
            pass
        except OSError:
            pass
        return hit

    def close(self) -> None:
        with self._mu:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
            self._fd = None
            self._wd_to_path.clear()
            self._path_to_wd.clear()


def wait_hot_change(path: str | Path, timeout_s: float = 1.0) -> bool:
    """Module-level helper for tail workers."""
    return get_hub().wait(path, timeout_s=timeout_s)
