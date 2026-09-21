"""Lifecycle commands written mid-turn must survive the post-response drain.

The post-response drain in turn_engine handles delay/ack itself and requeues a
short list of actions for the main loop. Anything in neither group is counted
as "processed" and silently discarded.

Astro hit this for real: a {"action":"restart"} written during a turn logged
"Post-response: drained 2 command(s)" and then nothing — no RESTART line, no
scheduler, no restart. Self-restart only worked if the agent happened to be
idle when the command landed, which is the case an agent cannot arrange for
itself, since it writes the command from inside a turn.
"""
import os
import re

TURN_ENGINE = os.path.join(os.path.dirname(__file__), "..", "core", "turn_engine.py")


def _requeued_actions():
    src = open(TURN_ENGINE).read()
    m = re.search(r"elif pa in \((.*?)\):", src, re.DOTALL)
    assert m, "post-response requeue branch not found"
    return m.group(1)


def test_restart_is_requeued_not_dropped():
    assert '"restart"' in _requeued_actions()


def test_shutdown_is_requeued_not_dropped():
    assert '"shutdown"' in _requeued_actions()


def test_previously_requeued_actions_still_are():
    actions = _requeued_actions()
    for a in ("compact", "gaze", "awareness", "reasoning_effort"):
        assert f'"{a}"' in actions
