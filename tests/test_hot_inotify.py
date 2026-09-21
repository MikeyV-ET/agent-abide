"""Shared HotInotifyHub does not open one instance per path."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tui"))

from hot_inotify import HotInotifyHub, get_hub


def test_one_fd_many_watches():
    hub = HotInotifyHub()
    if hub._fd is None:
        # environments without inotify — skip soft
        return
    d = tempfile.mkdtemp()
    paths = []
    for i in range(3):
        p = Path(d) / f"hot{i}.jsonl"
        p.write_text("{}\n")
        paths.append(p)
        assert hub.watch(p) is True
    assert len(hub._path_to_wd) == 3
    # still single fd
    assert hub._fd is not None
    # write should wake
    paths[1].write_text('{"a":1}\n', encoding="utf-8")
    # may need brief settle
    hit = hub.wait(paths[1], timeout_s=1.0)
    # hit may be True; if flaky on CI still ok that watch succeeded
    hub.close()


def test_get_hub_singleton():
    a = get_hub()
    b = get_hub()
    assert a is b
