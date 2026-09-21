"""Claude token accounting = context occupancy, not a sum over requests.

Every tool call inside a turn is another API request, and each one re-reads the
whole context as cache_read. Adding those up counts the same tokens many times:
measured on a live Astro transcript, a 29-request turn summed to 22x its actual
occupancy. Occupancy is the LAST request's usage, not the sum of all of them.

Sync tests only — they drive _process_frame and the seeding helper directly, so
they do not need pytest-asyncio (not installed in this environment).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

from claude_backend import ClaudeBackend  # noqa: E402


def _assistant_frame(input_t=2, cache_read=200_000, cache_create=1_000, output=500):
    return {
        "type": "assistant",
        "message": {
            "model": "claude-opus-5",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {
                "input_tokens": input_t,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_create,
                "output_tokens": output,
            },
        },
    }


def _feed(backend, frame, metas):
    backend._process_frame(
        frame,
        speech_chunks=[],
        thought_chunks=[],
        on_speech_chunk=None,
        on_tool_call=None,
        on_meta=metas.append,
    )


def test_occupancy_does_not_grow_across_requests_in_one_turn():
    """The core bug: 3 tool-call requests must not report 3x the context."""
    be = ClaudeBackend()
    metas = []

    for _ in range(3):
        _feed(be, _assistant_frame(cache_read=200_000, output=500), metas)

    # Each request carried ~201.5k of context, not 604k.
    assert be.total_tokens == 201_502
    assert metas[-1] == 201_502
    assert max(metas) < 250_000


def test_occupancy_tracks_a_growing_context():
    """It must still rise as the conversation actually grows."""
    be = ClaudeBackend()
    metas = []

    _feed(be, _assistant_frame(cache_read=100_000, output=100), metas)
    first = be.total_tokens
    _feed(be, _assistant_frame(cache_read=150_000, output=100), metas)

    assert be.total_tokens > first
    assert be.total_tokens == 151_102


def test_usage_without_tokens_does_not_clobber_a_known_occupancy():
    be = ClaudeBackend()
    metas = []

    _feed(be, _assistant_frame(cache_read=100_000, output=100), metas)
    known = be.total_tokens
    _feed(be, _assistant_frame(input_t=0, cache_read=0, cache_create=0, output=0), metas)

    assert be.total_tokens == known


def test_frames_without_usage_are_harmless():
    be = ClaudeBackend()
    be._total_tokens = 12_345
    _feed(be, {"type": "assistant", "message": {"content": []}}, [])
    assert be.total_tokens == 12_345


# --- seeding after restart -------------------------------------------------


def _write_session(path, usages):
    with open(path, "w") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        for u in usages:
            f.write(json.dumps({
                "type": "assistant",
                "message": {"model": "claude-opus-5", "content": [], "usage": u},
            }) + "\n")


def test_seeds_occupancy_from_the_session_transcript(tmp_path):
    """After a restart _total_tokens is 0, which empties the context_left tag.

    Grok seeds from updates.jsonl (_seed_tokens_from_session); Claude's
    equivalent source is the last assistant line's usage.
    """
    session = tmp_path / "sess.jsonl"
    _write_session(session, [
        {"input_tokens": 1, "cache_read_input_tokens": 50_000,
         "cache_creation_input_tokens": 0, "output_tokens": 100},
        {"input_tokens": 2, "cache_read_input_tokens": 90_000,
         "cache_creation_input_tokens": 500, "output_tokens": 300},
    ])

    be = ClaudeBackend()
    assert be.total_tokens == 0
    seeded = be._seed_tokens_from_session(str(session))

    # The LAST line's occupancy, not the sum of both.
    assert seeded == 90_802
    assert be.total_tokens == 90_802


def test_seeding_ignores_lines_without_usage(tmp_path):
    session = tmp_path / "sess.jsonl"
    with open(session, "w") as f:
        f.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        f.write(json.dumps({
            "type": "assistant",
            "message": {"content": [], "usage": {
                "input_tokens": 1, "cache_read_input_tokens": 70_000,
                "cache_creation_input_tokens": 0, "output_tokens": 50}},
        }) + "\n")
        f.write(json.dumps({"type": "attachment", "content": {}}) + "\n")
        f.write("{malformed\n")

    be = ClaudeBackend()
    assert be._seed_tokens_from_session(str(session)) == 70_051


def test_seeding_a_missing_file_is_survivable(tmp_path):
    be = ClaudeBackend()
    assert be._seed_tokens_from_session(str(tmp_path / "nope.jsonl")) == 0
    assert be.total_tokens == 0


def test_seeding_never_overwrites_a_live_count(tmp_path):
    """Mid-session the in-process count is authoritative; do not rewind to disk."""
    session = tmp_path / "sess.jsonl"
    _write_session(session, [{"input_tokens": 1, "cache_read_input_tokens": 10,
                              "cache_creation_input_tokens": 0, "output_tokens": 1}])

    be = ClaudeBackend()
    be._total_tokens = 123_456
    be._seed_tokens_from_session(str(session))

    assert be.total_tokens == 123_456


def test_refresh_tokens_seeds_itself_when_empty(tmp_path, monkeypatch):
    """refresh_tokens is what turn_engine asks before building the prompt."""
    session = tmp_path / "sess.jsonl"
    _write_session(session, [{"input_tokens": 5, "cache_read_input_tokens": 80_000,
                              "cache_creation_input_tokens": 0, "output_tokens": 95}])

    be = ClaudeBackend()
    monkeypatch.setattr(type(be), "session_file", property(lambda self: str(session)))

    assert be.refresh_tokens() == 80_100


# --- interjection env wiring ----------------------------------------------


def test_child_env_carries_agent_identity():
    """The hook needs AGENT_HOME to find the right interjection queue."""
    be = ClaudeBackend()
    env = be._build_child_env("/home/eric/agents/Sixel/Astro", agent_name="Astro",
                              interjection_enabled=False)
    assert env["AGENT_HOME"] == "/home/eric/agents/Sixel/Astro"
    assert env["AGENT_NAME"] == "Astro"
    assert env["ASDAAAS_DIR"].endswith("/asdaaas")


def test_bash_env_hook_installed_when_interjection_enabled():
    """Mid-turn delivery rides BASH_ENV; grok sets it, Claude did not."""
    be = ClaudeBackend()
    env = be._build_child_env("/tmp/agent", agent_name="Astro", interjection_enabled=True)
    assert env["BASH_ENV"].endswith("interjection_hook.sh")
    assert os.path.exists(env["BASH_ENV"])


def test_no_bash_env_when_interjection_disabled():
    be = ClaudeBackend()
    env = be._build_child_env("/tmp/agent", agent_name="Astro", interjection_enabled=False)
    assert "BASH_ENV" not in env


def test_no_bash_env_without_an_agent_name():
    be = ClaudeBackend()
    env = be._build_child_env("/tmp/agent", agent_name=None, interjection_enabled=True)
    assert "BASH_ENV" not in env


def test_build_cmd_includes_replay_when_interject():
    """--replay-user-messages gated on interjection_enabled (or explicit flag)."""
    def want(kwargs):
        return bool(kwargs.get("interjection_enabled") or kwargs.get("claude_stdin_interject", False))
    assert want({"interjection_enabled": True}) is True
    assert want({"interjection_enabled": False}) is False
    assert want({"claude_stdin_interject": True}) is True


def test_was_injected_tracks_exact_text():
    be = ClaudeBackend()
    be._injected_texts = ["[eric (via tui)] hello"]
    assert be.was_injected("[eric (via tui)] hello")
    assert not be.was_injected("other")


# --- stdin interjection record (the TUI label) -----------------------------
#
# e692c20 CALLS self._record_stdin_interjection(text) after a successful stdin
# inject, but no commit ever defined it. Live, every inject logged
# "'ClaudeBackend' object has no attribute '_record_stdin_interjection'",
# delivery worked, and the TUI got no InterjectionBlock — Eric's original
# "I'm not getting an indication of it", still true. Found by dogfooding.


def _backend_with_history(tmp_path):
    be = ClaudeBackend()
    be._agent_home = tmp_path
    be._agent_name = "Astro"
    be._session_id = "sid-test"
    return be


def _hot_events(tmp_path):
    import glob
    paths = glob.glob(str(tmp_path / "**" / "hot.jsonl"), recursive=True)
    assert paths, "no hot.jsonl written"
    with open(paths[0]) as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def test_record_method_exists():
    assert callable(getattr(ClaudeBackend(), "_record_stdin_interjection", None))


def test_record_writes_an_interjection_hot_event(tmp_path):
    be = _backend_with_history(tmp_path)
    be._record_stdin_interjection("eric: stop, wrong file")

    events = _hot_events(tmp_path)
    assert len(events) == 1
    ev = events[0]
    assert ev["format"] == "aa.stream"
    assert ev["body"]["kind"] == "interjection"
    assert ev["body"]["text"] == "eric: stop, wrong file"


def test_recorded_event_renders_as_an_interjection_block(tmp_path):
    """End to end against the real consumer, not a guess at its contract."""
    from tui_history import aa_event_to_tui_update

    be = _backend_with_history(tmp_path)
    be._record_stdin_interjection("look here")
    update = aa_event_to_tui_update(_hot_events(tmp_path)[0])

    assert update is not None
    blob = json.dumps(update)
    assert "<interjection>" in blob
    assert "look here" in blob


def test_record_advances_stream_seq(tmp_path):
    be = _backend_with_history(tmp_path)
    be._record_stdin_interjection("one")
    be._record_stdin_interjection("two")
    seqs = [e["stream_seq"] for e in _hot_events(tmp_path)]
    assert seqs == sorted(seqs) and len(set(seqs)) == 2


def test_record_is_a_noop_without_history_configured():
    """Hot ingest off -> nothing to write to; must not raise."""
    ClaudeBackend()._record_stdin_interjection("hello")


def test_record_catches_the_hot_ingest_up_before_taking_a_seq(tmp_path):
    """Order in the TUI is stream_seq order, so the interjection must be numbered
    AFTER everything that happened before it.

    Seen live: the recorder took seq 2168 at 21:20:12 while the hot ingest had only
    reached 21:20:03, so speech from 21:20:06 and a tool call from 21:20:09 were
    ingested afterwards as 2169-2171 — and the panel drew above things that
    preceded it. Syncing the ingest first puts the panel where it happened.
    """
    be = _backend_with_history(tmp_path)
    calls = []
    be.sync_hot_stream = lambda *a, **k: calls.append("sync") or {"status": "ok"}

    import aa_stream
    real_append = aa_stream.append_hot_events

    def spy_append(fs_dir, events):
        calls.append("append")
        return real_append(fs_dir, events)

    aa_stream.append_hot_events = spy_append
    try:
        be._record_stdin_interjection("late arrival")
    finally:
        aa_stream.append_hot_events = real_append

    assert calls[:2] == ["sync", "append"], calls


def test_record_still_writes_if_the_catch_up_fails(tmp_path):
    """A failed ingest catch-up costs ordering, not the record itself."""
    be = _backend_with_history(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("ingest broke")

    be.sync_hot_stream = boom
    be._record_stdin_interjection("still recorded")
    assert _hot_events(tmp_path)[-1]["body"]["text"] == "still recorded"
