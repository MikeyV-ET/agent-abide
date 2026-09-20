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
