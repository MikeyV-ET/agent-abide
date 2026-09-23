"""Claude compaction reporting: the numbers must come from Claude's own record.

Live on 2026-09-22 an agent-initiated /compact worked -- 878,559 tokens down to
9,961 -- and the notice handed back to the agent read "Context reduced from
878095 to 878095 tokens" with a context_left tag of "0.0k till autocompaction".
ClaudeBackend never implemented pop_compaction_event(), so turn_engine's

    tokens_before = event_tb or tokens_before
    self.total_tokens = event_ta or self.total_tokens

kept the pre-compaction values from the base class's (False, None, 0). The true
numbers were sitting in the transcript's compact_boundary record the whole time.

Sync tests only -- they drive the backend directly, so no pytest-asyncio.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

from agent_backend import AgentBackend  # noqa: E402
from claude_backend import ClaudeBackend  # noqa: E402


def _boundary(pre=878_559, post=9_961, uuid="b-1", trigger="manual", meta=True,
              timestamp="2026-09-22T20:53:26.946Z"):
    """The shape Claude Code actually writes (fields it does not use trimmed)."""
    rec = {
        "type": "system",
        "subtype": "compact_boundary",
        "content": "Conversation compacted",
        "isMeta": False,
        "timestamp": timestamp,
        "uuid": uuid,
    }
    if meta:
        rec["compactMetadata"] = {
            "trigger": trigger,
            "preTokens": pre,
            "postTokens": post,
            "durationMs": 115040,
            "cumulativeDroppedTokens": pre - post,
        }
    return rec


def _session(tmp_path, *records):
    path = tmp_path / "sess.jsonl"
    with open(path, "w") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return path


def _backend(tmp_path, monkeypatch, *records, floor=0.0):
    """floor defaults to 0 so fixture boundaries count as "since we started"."""
    be = ClaudeBackend()
    be._compaction_floor = floor
    path = _session(tmp_path, *records)
    monkeypatch.setattr(type(be), "session_file", property(lambda self: str(path)))
    return be


def test_reports_the_exact_pre_and_post_token_counts(tmp_path, monkeypatch):
    be = _backend(tmp_path, monkeypatch, _boundary())
    landed, after, before = be.pop_compaction_event()

    assert landed is True
    assert (before, after) == (878_559, 9_961)


def test_the_notice_arithmetic_no_longer_degenerates(tmp_path, monkeypatch):
    """Regression on the live failure: before and after must not be equal.

    Mirrors turn_engine's `event_tb or tokens_before` / `event_ta or total`.
    """
    be = _backend(tmp_path, monkeypatch, _boundary())
    be._total_tokens = 878_095  # asdaaas's stale pre-compaction count
    tokens_before = 878_095

    _, event_ta, event_tb = be.pop_compaction_event()
    tokens_before = event_tb or tokens_before
    tokens_after = event_ta or be.total_tokens

    assert tokens_before == 878_559
    assert tokens_after == 9_961
    assert tokens_after < tokens_before


def test_lowers_a_stale_occupancy_so_the_context_tag_is_honest(tmp_path, monkeypatch):
    """_total_tokens stays pre-compaction until the next assistant frame.

    context_left_tag() reads it, which is how a freshly emptied context got
    labelled "0.0k till autocompaction".
    """
    be = _backend(tmp_path, monkeypatch, _boundary())
    be._total_tokens = 878_095
    be.pop_compaction_event()

    assert be.total_tokens == 9_961


def test_each_boundary_is_reported_only_once(tmp_path, monkeypatch):
    """The poll loop calls this repeatedly while it waits."""
    be = _backend(tmp_path, monkeypatch, _boundary())

    assert be.pop_compaction_event()[0] is True
    assert be.pop_compaction_event() == (False, None, 0)


def test_the_newest_boundary_wins(tmp_path, monkeypatch):
    be = _backend(tmp_path, monkeypatch,
                  _boundary(pre=500_000, post=40_000, uuid="old"),
                  _boundary(pre=878_559, post=9_961, uuid="new"))

    assert be.pop_compaction_event() == (True, 9_961, 878_559)


def test_an_auto_compaction_is_reported_too(tmp_path, monkeypatch):
    """trigger distinguishes manual from auto; both are real compactions."""
    be = _backend(tmp_path, monkeypatch, _boundary(trigger="auto"))
    assert be.pop_compaction_event()[0] is True


def test_no_compaction_reports_nothing(tmp_path, monkeypatch):
    be = _backend(tmp_path, monkeypatch,
                  {"type": "assistant", "message": {"content": []}})
    assert be.pop_compaction_event() == (False, None, 0)


def test_a_boundary_without_metadata_is_not_trusted(tmp_path, monkeypatch):
    be = _backend(tmp_path, monkeypatch, _boundary(meta=False))
    assert be.pop_compaction_event() == (False, None, 0)


def test_partial_token_metadata_is_not_trusted(tmp_path, monkeypatch):
    rec = _boundary()
    del rec["compactMetadata"]["postTokens"]
    be = _backend(tmp_path, monkeypatch, rec)
    assert be.pop_compaction_event() == (False, None, 0)


def test_a_truncated_line_does_not_raise(tmp_path, monkeypatch):
    """The scan window can start mid-line, and writes can be partial."""
    path = tmp_path / "sess.jsonl"
    with open(path, "w") as f:
        f.write(json.dumps(_boundary()) + "\n")
        f.write('{"subtype":"compact_boundary","compactMe\n')

    be = ClaudeBackend()
    be._compaction_floor = 0.0
    monkeypatch.setattr(type(be), "session_file", property(lambda self: str(path)))
    assert be.pop_compaction_event() == (True, 9_961, 878_559)


def test_a_missing_session_file_is_survivable(tmp_path, monkeypatch):
    be = ClaudeBackend()
    monkeypatch.setattr(type(be), "session_file",
                        property(lambda self: str(tmp_path / "nope.jsonl")))
    assert be.pop_compaction_event() == (False, None, 0)


def test_no_session_file_at_all_is_survivable():
    assert ClaudeBackend().pop_compaction_event() == (False, None, 0)


# --- poll budget -----------------------------------------------------------


def test_claude_declares_a_poll_budget_longer_than_it_takes_to_compact():
    """Measured durationMs 115040; the 30s default gave up mid-compaction."""
    assert ClaudeBackend.compaction_poll_seconds > 115


def test_raising_claudes_budget_does_not_move_the_default():
    """Grok's 30s must be unchanged -- the budget is per-backend."""
    assert AgentBackend.compaction_poll_seconds == 30


# --- staleness across a restart --------------------------------------------
#
# Shipped in bfc940b with uuid dedup only, and it bit that same evening. asdaaas
# restarted at 18:50 on a fresh process, _last_compaction_uuid was None, and the
# only compact_boundary in the transcript was five hours old. pop_compaction_event
# handed it back as current, so the agent was told "[Compaction complete. Context
# reduced from 878559 to 9961 tokens]" when nothing had compacted -- and occupancy
# was set to a stale 9,961 against a real ~90k. I had noted this exact risk when
# writing it and shipped anyway as "acceptable".


def _ts(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def test_a_boundary_from_before_this_process_is_not_reported(tmp_path, monkeypatch):
    """The live regression: old compaction, new process, nothing compacted."""
    import time as _time
    start = _time.time()
    be = _backend(tmp_path, monkeypatch,
                  _boundary(timestamp=_ts(start - 5 * 3600)),
                  floor=start)

    assert be.pop_compaction_event() == (False, None, 0)


def test_a_stale_boundary_does_not_clobber_occupancy(tmp_path, monkeypatch):
    """Reporting the wrong tokens is bad; writing them to occupancy is worse."""
    import time as _time
    start = _time.time()
    be = _backend(tmp_path, monkeypatch,
                  _boundary(timestamp=_ts(start - 5 * 3600)),
                  floor=start)
    be._total_tokens = 90_752
    be.pop_compaction_event()

    assert be.total_tokens == 90_752


def test_a_boundary_from_after_we_started_is_still_reported(tmp_path, monkeypatch):
    """The gate must not swallow the real case it exists to protect."""
    import time as _time
    start = _time.time()
    be = _backend(tmp_path, monkeypatch,
                  _boundary(timestamp=_ts(start + 30)),
                  floor=start)

    assert be.pop_compaction_event() == (True, 9_961, 878_559)


def test_an_undateable_boundary_is_still_reported(tmp_path, monkeypatch):
    """Only a boundary we can date can be ruled out; no timestamp -> trust it."""
    import time as _time
    be = _backend(tmp_path, monkeypatch,
                  _boundary(timestamp=""), floor=_time.time())

    assert be.pop_compaction_event() == (True, 9_961, 878_559)


def test_a_malformed_timestamp_is_still_reported(tmp_path, monkeypatch):
    import time as _time
    be = _backend(tmp_path, monkeypatch,
                  _boundary(timestamp="not-a-date"), floor=_time.time())

    assert be.pop_compaction_event() == (True, 9_961, 878_559)


def test_the_floor_exists_without_ever_calling_start():
    """A backend that is constructed but never started still has a floor."""
    import time as _time
    be = ClaudeBackend()
    assert be._compaction_floor > _time.time() - 60
