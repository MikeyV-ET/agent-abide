"""turn_engine's command drains must never drop an action they do not handle.

turn_engine drains the command queue at four points (post-response, late, straggler,
pre-continue). Each used to carry its own list of actions to hand back to the main
loop, and the lists had drifted apart:

    post-response   compact gaze awareness reasoning_effort
    late            compact gaze awareness reasoning_effort
    pre-continue    compact gaze awareness
    straggler       (nothing)

Anything missing from a site's list was counted as handled and discarded. Astro hit
it twice in one evening: {"action": "restart"} vanished at the post-response drain,
and once that was patched it vanished at the late drain instead, because fixing one
list left the other three wrong.

The fix inverts the rule: turn_engine handles delay and ack, and hands everything
else back. These tests check that rule, not any one list.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

import turn_engine  # noqa: E402

TURN_ENGINE = os.path.join(os.path.dirname(__file__), "..", "core", "turn_engine.py")


def _point_agent_dir_at(monkeypatch, tmp_path):
    import asdaaas
    monkeypatch.setattr(asdaaas, "agent_dir", lambda name, env=None: tmp_path)


def _requeued(tmp_path):
    out = []
    for p in sorted((tmp_path / "commands").glob("cmd_requeue_*.json")):
        with open(p) as f:
            out.append(json.load(f))
    return out


def test_only_delay_and_ack_are_handled_locally():
    assert turn_engine.LOCAL_ACTIONS == frozenset({"delay", "ack"})


def test_lifecycle_commands_are_handed_back(monkeypatch, tmp_path):
    _point_agent_dir_at(monkeypatch, tmp_path)
    cmds = [{"action": "restart", "reason": "x"}, {"action": "shutdown"}]

    assert turn_engine.requeue_for_main_loop("Astro", cmds) == 2
    assert {c["action"] for c in _requeued(tmp_path)} == {"restart", "shutdown"}


def test_actions_nobody_listed_are_handed_back_too(monkeypatch, tmp_path):
    """The point of inverting: a NEW action works without editing four lists."""
    _point_agent_dir_at(monkeypatch, tmp_path)
    turn_engine.requeue_for_main_loop("Astro", [{"action": "some_future_action"}])
    assert _requeued(tmp_path)[0]["action"] == "some_future_action"


def test_locally_handled_actions_are_not_requeued(monkeypatch, tmp_path):
    """Requeueing delay/ack would replay them forever."""
    _point_agent_dir_at(monkeypatch, tmp_path)
    n = turn_engine.requeue_for_main_loop(
        "Astro", [{"action": "delay", "seconds": 5}, {"action": "ack", "handled": ["b1"]}]
    )
    assert n == 0
    assert _requeued(tmp_path) == []


def test_no_drain_carries_its_own_whitelist_any_more():
    """The drift came from per-site lists; none may come back."""
    src = open(TURN_ENGINE).read()
    assert not re.search(r'in \(\s*"compact"', src), "a per-site requeue whitelist has returned"
