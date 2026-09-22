"""inotify → debounced sync for updates → hot."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from updates_hot_watch import UpdatesHotWatcher  # noqa: E402


def test_watcher_debounced_sync(tmp_path: Path):
    updates = tmp_path / "updates.jsonl"
    updates.write_text("{}\n")
    calls = []

    def sync():
        calls.append(time.time())
        return {"status": "ok"}

    w = UpdatesHotWatcher(updates, sync, debounce_s=0.1, idle_poll_s=5.0, name="test")
    w.start()
    time.sleep(0.05)  # initial sync
    n0 = len(calls)
    assert n0 >= 1  # initial catch-up
    updates.write_text(updates.read_text() + '{"x":1}\n')
    # wait debounce + sync
    deadline = time.time() + 2.0
    while time.time() < deadline and len(calls) <= n0:
        time.sleep(0.05)
    w.stop()
    assert len(calls) > n0, f"expected sync after write, calls={calls}"


def test_kick_without_inotify(tmp_path: Path):
    updates = tmp_path / "updates.jsonl"
    updates.write_text("{}\n")
    calls = []

    def sync():
        calls.append(1)
        return {"status": "ok"}

    w = UpdatesHotWatcher(updates, sync, debounce_s=0.05, idle_poll_s=30.0, name="kick")
    w._poll_only = True  # force poll path still works with kick via dirty
    w.start()
    time.sleep(0.1)
    n0 = len(calls)
    w.kick()
    deadline = time.time() + 1.5
    while time.time() < deadline and len(calls) <= n0:
        time.sleep(0.05)
    w.stop()
    assert len(calls) > n0
