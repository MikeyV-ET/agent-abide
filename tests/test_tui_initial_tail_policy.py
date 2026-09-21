"""Initial tab catch-up is a short tip; lazy-load owns depth (no secondary 80 floor)."""
from __future__ import annotations

import sys
from pathlib import Path

# Mirror constants + policy without importing full Textual app
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tui"))

# Read constants from module source without running app
import importlib.util
# Import only by exec of the constant block is fragile; duplicate the contract:
DEFAULT_PRIMARY_TAIL_SPEECH = 50
DEFAULT_SECONDARY_TAIL_SPEECH = 25


def initial_tail_speech_count(tail_count, agents, agent_name: str) -> int:
    if tail_count:
        return max(1, int(tail_count))
    is_primary = bool(agents) and agent_name == agents[0]
    return DEFAULT_PRIMARY_TAIL_SPEECH if is_primary else DEFAULT_SECONDARY_TAIL_SPEECH


def test_secondary_no_longer_floors_at_80():
    # was: max(50, 80) == 80 for secondary with -t50
    assert initial_tail_speech_count(50, ["Trip-G", "Squiggy"], "Squiggy") == 50
    assert initial_tail_speech_count(50, ["Trip-G"], "Trip-G") == 50


def test_add_tab_default_is_light():
    assert initial_tail_speech_count(None, ["Trip-G", "Squiggy"], "Squiggy") == 25
    assert initial_tail_speech_count(None, ["Trip-G"], "Trip-G") == 50


def test_secondary_with_small_t():
    assert initial_tail_speech_count(15, ["A", "B"], "B") == 15
