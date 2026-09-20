"""Optional sidecar stream tailer (orchestration).

**Preferred owner:** AA backends (``GrokBackend.sync_hot_stream`` /
``ClaudeBackend.sync_hot_stream``) — acquire native session file, normalize
via ``stream_adapters.*``, write ``aa_stream.append_hot_events``.

This module is the **fallback sidecar** when ``history/config.json`` sets
``owner: hook|sidecar``. Same adapters + checkpoints; do not run both as
primary writers (shared checkpoint makes double-append a no-op, but wasteful).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import fcntl
import json
import os
import select
import struct
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_lock = threading.Lock()
_threads: dict[str, threading.Thread] = {}
_stop_flags: dict[str, threading.Event] = {}

_IN_MODIFY = 0x00000002
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVE_SELF = 0x00000800
_IN_DELETE_SELF = 0x00000400


def _agent_home(agent_name: str, env=None) -> Path:
    try:
        from asdaaas import agent_dir
        return agent_dir(agent_name, env=env).parent
    except Exception:
        return Path.home() / "agents" / agent_name


def load_full_stream_config(agent_name: str, env=None) -> Optional[Dict[str, Any]]:
    """Load history/config.json (canonical) or legacy full_stream/config.json."""
    home = _agent_home(agent_name, env)
    for rel in ("history/config.json", "full_stream/config.json"):
        cfg_path = home / "asdaaas" / rel
        if not cfg_path.is_file():
            continue
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[full_stream_hook] bad config {cfg_path}: {e}")
            return None
    return None


def maybe_tail_grok_after_turn(
    agent_name: str, env=None, session_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """One-shot drain (end of turn safety net). Name kept; any backend."""
    cfg = load_full_stream_config(agent_name, env)
    if not _stream_tail_enabled(cfg):
        return None
    return _do_tail(agent_name, env=env, session_id=session_id, cfg=cfg)


def _stream_tail_enabled(cfg: Optional[Dict[str, Any]]) -> bool:
    """True if history config wants native→hot tailing.

    Accepts legacy ``tail_grok`` and generic ``tail_stream`` / ``tail_backend``.
    """
    if not cfg:
        return False
    if cfg.get("tail_stream") or cfg.get("tail_backend"):
        return True
    return bool(cfg.get("tail_grok"))


def resolve_agent_backend(agent_name: str, env=None, cfg: Optional[Dict[str, Any]] = None) -> str:
    """backend from history config, agents.json, or health.json (default grok)."""
    cfg = cfg or {}
    b = (cfg.get("backend") or "").strip().lower()
    if b:
        return b
    # agents.json
    try:
        import json as _json
        home = Path.home()
        for cand in (
            home / "agents" / "config" / "agents.json",
            Path(os.environ.get("ASDAAAS_CONFIG") or "") if os.environ.get("ASDAAAS_CONFIG") else None,
        ):
            if cand is None:
                continue
            path = cand if cand.is_file() else (cand / "agents.json" if cand.is_dir() else None)
            if path is None or not path.is_file():
                continue
            data = _json.loads(path.read_text(encoding="utf-8"))
            ac = (data.get("agents") or {}).get(agent_name) or {}
            if ac.get("backend"):
                return str(ac["backend"]).lower()
    except Exception:
        pass
    # health.json
    try:
        import json as _json
        hp = _agent_home(agent_name, env) / "asdaaas" / "health.json"
        if hp.is_file():
            h = _json.loads(hp.read_text(encoding="utf-8"))
            if h.get("backend"):
                return str(h["backend"]).lower()
    except Exception:
        pass
    return "grok"


def _do_tail(
    agent_name: str,
    *,
    env=None,
    session_id: Optional[str] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = cfg or load_full_stream_config(agent_name, env) or {}
    try:
        from stream_adapters import tail_once_for_backend
    except Exception as e:
        print(f"[full_stream_hook] import stream_adapters failed: {e}")
        return {"status": "error", "error": f"import: {e}"}

    home = _agent_home(agent_name, env)
    backend = resolve_agent_backend(agent_name, env=env, cfg=cfg)
    max_bytes = cfg.get("max_bytes_per_tick")
    max_lines = cfg.get("max_lines_per_tick")
    try:
        result = tail_once_for_backend(
            backend,
            home,
            agent_name,
            session_id=session_id or cfg.get("session_id"),
            max_lines=max_lines,
            max_bytes=int(max_bytes) if max_bytes else None,
        )
        n = result.get("lines_ingested") or 0
        if n:
            print(
                f"[full_stream_hook] {agent_name}/{backend}: tailed {n} lines "
                f"→ hot {result.get('hot_bytes')}B"
            )
        return result
    except Exception as e:
        print(f"[full_stream_hook] {agent_name} tail failed: {e}")
        traceback.print_exc()
        return {"status": "error", "error": str(e)}


def _resolve_native_path(
    agent_name: str,
    env=None,
    session_id: Optional[str] = None,
    backend: Optional[str] = None,
) -> Optional[Path]:
    """Native session file to watch (updates.jsonl or claude session jsonl)."""
    home = _agent_home(agent_name, env)
    backend = (backend or resolve_agent_backend(agent_name, env=env)).lower()
    try:
        from full_stream import resolve_history_dir
        from aa_stream import read_checkpoint

        if backend in ("claude", "anthropic"):
            from stream_adapters.claude import find_live_session

            p = find_live_session(home, session_id)
            if p and p.exists():
                return Path(p).resolve()
            ck = read_checkpoint(resolve_history_dir(home), "claude")
        else:
            from aa_stream import find_live_updates

            p = find_live_updates(home, session_id)
            if p and p.exists():
                return Path(p).resolve()
            ck = read_checkpoint(resolve_history_dir(home), "grok")
        if ck.get("path"):
            cp = Path(ck["path"])
            if cp.exists():
                return cp.resolve()
    except Exception as e:
        print(f"[full_stream_hook] resolve native path failed: {e}")
    return None


def _resolve_updates_path(agent_name: str, env=None, session_id: Optional[str] = None) -> Optional[Path]:
    """Back-compat alias: native source path for inotify."""
    return _resolve_native_path(agent_name, env=env, session_id=session_id)


class _UpdatesInotify:
    """One inotify fd watching a single updates.jsonl path."""

    def __init__(self):
        self._libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self._libc.inotify_init.restype = ctypes.c_int
        self._libc.inotify_add_watch.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32
        ]
        self._libc.inotify_add_watch.restype = ctypes.c_int
        self.fd: Optional[int] = None
        self.wd: Optional[int] = None
        self.path: Optional[Path] = None

    def arm(self, path: Path) -> bool:
        path = Path(path).resolve()
        if self.fd is not None and self.path == path and self.wd is not None:
            return True
        self.close()
        fd = self._libc.inotify_init()
        if fd < 0:
            print(
                f"[full_stream_hook] inotify_init failed errno={ctypes.get_errno()}"
            )
            return False
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        mask = _IN_MODIFY | _IN_CLOSE_WRITE | _IN_MOVE_SELF | _IN_DELETE_SELF
        wd = self._libc.inotify_add_watch(fd, str(path).encode(), mask)
        if wd < 0:
            print(
                f"[full_stream_hook] inotify_add_watch failed path={path} "
                f"errno={ctypes.get_errno()}"
            )
            os.close(fd)
            return False
        self.fd = fd
        self.wd = wd
        self.path = path
        print(f"[full_stream_hook] inotify armed on {path} (fd={fd} wd={wd})")
        return True

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
        self.wd = None
        self.path = None

    def wait(self, timeout_s: float) -> Tuple[bool, bool]:
        """
        Wait for event or timeout.
        Returns (saw_event, need_rearm) — need_rearm if file deleted/moved.
        """
        if self.fd is None:
            time.sleep(timeout_s)
            return False, False
        try:
            r, _, _ = select.select([self.fd], [], [], max(0.0, timeout_s))
        except (ValueError, OSError):
            time.sleep(min(timeout_s, 0.1))
            return False, True
        if not r:
            return False, False
        need_rearm = False
        try:
            while True:
                data = os.read(self.fd, 65536)
                if not data:
                    break
                i = 0
                while i + 16 <= len(data):
                    wd, mask, cookie, name_len = struct.unpack_from("iIII", data, i)
                    i += 16 + name_len
                    if mask & (_IN_DELETE_SELF | _IN_MOVE_SELF):
                        need_rearm = True
        except BlockingIOError:
            pass
        except OSError:
            need_rearm = True
        return True, need_rearm


def start_background_tailer(
    agent_name: str,
    env=None,
    session_id: Optional[str] = None,
) -> bool:
    """
    Start daemon thread: inotify (or poll) on updates.jsonl → hot.jsonl.
    Idempotent per agent_name.
    """
    cfg = load_full_stream_config(agent_name, env)
    if not _stream_tail_enabled(cfg):
        return False

    with _lock:
        t = _threads.get(agent_name)
        if t is not None and t.is_alive():
            return True
        stop = threading.Event()
        _stop_flags[agent_name] = stop

        def run():
            use_inotify = cfg.get("use_inotify", True)
            # safety-net timeout when quiet (also used as pure-poll interval)
            def safety_s(c: dict) -> float:
                try:
                    ms = int(c.get("interval_ms") or 1000)
                except (TypeError, ValueError):
                    ms = 1000
                return max(0.2, min(ms / 1000.0, 5.0))

            watcher = _UpdatesInotify() if use_inotify else None
            mode = "inotify" if use_inotify else "poll"
            print(
                f"[full_stream_hook] {agent_name}: background tailer started ({mode})"
            )

            while not stop.is_set():
                cfg_now = load_full_stream_config(agent_name, env)
                if not _stream_tail_enabled(cfg_now):
                    print(
                        f"[full_stream_hook] {agent_name}: stream tail off — background exit"
                    )
                    break

                backend = resolve_agent_backend(agent_name, env=env, cfg=cfg_now)
                src = _resolve_native_path(
                    agent_name, env=env, session_id=session_id, backend=backend
                )
                if not src:
                    stop.wait(1.0)
                    continue

                if watcher is not None:
                    if not watcher.arm(src):
                        # fall back to poll this process forever
                        print(
                            f"[full_stream_hook] {agent_name}: "
                            "inotify failed — poll fallback"
                        )
                        watcher.close()
                        watcher = None

                if watcher is not None:
                    saw, rearm = watcher.wait(safety_s(cfg_now))
                    if rearm:
                        watcher.close()
                    # drain whether event or timeout
                    _do_tail(
                        agent_name, env=env, session_id=session_id, cfg=cfg_now
                    )
                else:
                    _do_tail(
                        agent_name, env=env, session_id=session_id, cfg=cfg_now
                    )
                    stop.wait(safety_s(cfg_now))

            if watcher is not None:
                watcher.close()
            print(f"[full_stream_hook] {agent_name}: background tailer stopped")

        th = threading.Thread(
            target=run,
            name=f"full-stream-tail-{agent_name}",
            daemon=True,
        )
        _threads[agent_name] = th
        th.start()
        return True


def stop_background_tailer(agent_name: str) -> None:
    with _lock:
        stop = _stop_flags.get(agent_name)
        if stop:
            stop.set()
        t = _threads.get(agent_name)
    if t and t.is_alive():
        t.join(timeout=2.0)
    with _lock:
        _threads.pop(agent_name, None)
        _stop_flags.pop(agent_name, None)
