"""aa-dev TUI history resolve is hot.jsonl only."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from tui_history import resolve_history_source, hot_jsonl_path  # noqa: E402


def test_resolve_never_returns_updates_even_if_present(tmp_path: Path):
    home = tmp_path / "Agent"
    (home / "asdaaas" / "history").mkdir(parents=True)
    (home / "asdaaas" / "updates.jsonl").write_text('{"x":1}\n')
    # no hot yet
    kind, path = resolve_history_source(home, prefer="auto")
    assert kind == "none"
    assert path == hot_jsonl_path(home)
    # empty hot counts as hot SoR
    hot_jsonl_path(home).write_text("")
    kind, path = resolve_history_source(home, prefer="auto")
    assert kind == "hot"
    assert path == hot_jsonl_path(home)
    # prefer=updates explicitly refused
    kind2, _ = resolve_history_source(home, prefer="updates")
    assert kind2 == "none"


def test_resolve_hot_when_present(tmp_path: Path):
    home = tmp_path / "Agent"
    (home / "asdaaas" / "history").mkdir(parents=True)
    hot_jsonl_path(home).write_text('{"format":"aa.stream"}\n')
    (home / "asdaaas" / "updates.jsonl").write_text("legacy\n")
    kind, path = resolve_history_source(home)
    assert kind == "hot"
    assert path.name == "hot.jsonl"
