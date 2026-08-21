"""After session load, token count must be seeded so context_left_tag is non-empty."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from asdaaas import context_left_tag
from grok_backend import GrokBackend


def test_context_left_tag_empty_when_tokens_zero():
    assert context_left_tag(0, 500_000) == ""
    assert context_left_tag(100_000, 500_000) != ""
    assert "Context left" in context_left_tag(100_000, 500_000)


def test_seed_tokens_from_updates_tail(tmp_path):
    session = tmp_path / "sess"
    session.mkdir()
    updates = session / "updates.jsonl"
    # noise + last totalTokens
    frames = [
        {"params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}}}},
        {"params": {"update": {"sessionUpdate": "x", "_meta": {"totalTokens": 50_000}}}},
        {"params": {"update": {"sessionUpdate": "y", "_meta": {"totalTokens": 123_456}}}},
    ]
    updates.write_text("\n".join(json.dumps(f) for f in frames) + "\n")

    be = GrokBackend()
    be._session_dir = session
    be._total_tokens = 0
    be._seed_tokens_from_session()
    assert be._total_tokens == 123_456
    tag = context_left_tag(be._total_tokens, 500_000, turns_since_compaction=5, gaze=None)
    assert tag
    assert "Context left" in tag


def test_seed_prefers_compaction_tokens_after(tmp_path):
    session = tmp_path / "sess"
    session.mkdir()
    updates = session / "updates.jsonl"
    frames = [
        {"params": {"update": {"sessionUpdate": "x", "_meta": {"totalTokens": 400_000}}}},
        {"params": {"update": {
            "sessionUpdate": "auto_compact_completed",
            "tokens_before": 400_000,
            "tokens_after": 80_000,
        }}},
    ]
    updates.write_text("\n".join(json.dumps(f) for f in frames) + "\n")
    be = GrokBackend()
    be._session_dir = session
    be._seed_tokens_from_session()
    assert be._total_tokens == 80_000


def test_seed_noop_on_empty(tmp_path):
    session = tmp_path / "sess"
    session.mkdir()
    (session / "updates.jsonl").write_text("")
    be = GrokBackend()
    be._session_dir = session
    be._total_tokens = 0
    be._seed_tokens_from_session()
    assert be._total_tokens == 0
