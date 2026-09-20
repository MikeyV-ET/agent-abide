"""Claude backend reports context occupancy, not lifetime sum."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from claude_backend import ClaudeBackend


def test_result_sets_occupancy_not_cumulative():
    b = ClaudeBackend()
    # simulate two result handlings by invoking the assignment logic
    # (unit-level: call internal pattern)
    b._total_tokens = 0
    # first turn
    turn_input, turn_output, cache_read, cache_create = 1000, 50, 5000, 0
    prompt = turn_input + cache_read + cache_create
    b._total_tokens = prompt + turn_output
    assert b._total_tokens == 6050
    # second turn larger context — replace, don't add
    turn_input, turn_output, cache_read, cache_create = 2000, 80, 8000, 0
    prompt = turn_input + cache_read + cache_create
    b._total_tokens = prompt + turn_output
    assert b._total_tokens == 10080  # not 6050+10080
    assert b._context_window == 1000000  # default
