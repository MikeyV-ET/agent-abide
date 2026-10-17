"""Unit tests for TUI Grok STT helper (no live mic / network)."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))
from voice_stt import (
    VoiceError,
    build_stt_ws_url,
    commit_live,
    fold_into_clip,
    merge_voice_fragment,
    recorder_candidates,
    resolve_bearer,
)


def test_build_stt_ws_url_default():
    url = build_stt_ws_url()
    assert url.startswith("wss://api.x.ai/v1/stt?")
    assert "sample_rate=16000" in url
    assert "encoding=pcm" in url
    assert "language=en" in url
    assert "interim_results=true" in url
    assert "endpointing=400" in url


def test_build_stt_ws_url_v1_dedupe():
    url = build_stt_ws_url("https://proxy.example.com/v1")
    assert url.startswith("wss://proxy.example.com/v1/stt?")


def test_build_stt_ws_url_rejects_http():
    with pytest.raises(VoiceError):
        build_stt_ws_url("http://localhost:8080")


def test_auto_language_becomes_en():
    url = build_stt_ws_url(language="auto")
    assert "language=en" in url


def test_merge_voice_fragment():
    assert merge_voice_fragment("", "hello") == "hello"
    assert merge_voice_fragment("hi", "there") == "hi there"
    assert merge_voice_fragment("hi ", "there") == "hi there"


def test_fold_into_clip_extends_and_does_not_repeat():
    clip = fold_into_clip("", "I just restarted")
    clip = fold_into_clip(clip, "I just restarted")
    clip = fold_into_clip(clip, "I just restarted it")
    clip = fold_into_clip(clip, "I just restarted it")
    assert clip == "I just restarted it"
    clip = fold_into_clip(clip, "Why are we doing F8 and escape")
    clip = fold_into_clip(clip, "Why are we doing F8 and escape")
    clip = fold_into_clip(clip, "Why are we doing F8 and escape to stop")
    assert clip == "I just restarted it Why are we doing F8 and escape to stop"


def test_growing_partials_are_one_utterance_until_speech_final():
    """API sends the same take growing over 1–3s; only speech_final commits."""
    live = "Okay, I"
    live = "Okay, I restarted."
    live = "Okay, I restarted. Let's take a look and see what happens."
    clip = commit_live("", live)
    clip = commit_live(clip, live)
    assert clip == live


def test_commit_live_skips_duplicate_and_keeps_correction_as_one_utterance():
    clip = ""
    live = "Okay, are we"
    live = "Okay, I restarted again"
    clip = commit_live(clip, live)
    assert clip == "Okay, I restarted again"
    clip = commit_live(clip, "Okay, I restarted again")
    assert clip == "Okay, I restarted again"
    clip = commit_live(clip, "So is it possible")
    assert clip == "Okay, I restarted again So is it possible"


def test_fold_into_clip_overlap_on_pause():
    clip = fold_into_clip("", "hello world")
    clip = fold_into_clip(clip, "world after the pause")
    assert clip == "hello world after the pause"


def test_resolve_bearer_env(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "  sk-test  ")
    assert resolve_bearer() == "sk-test"


def test_resolve_bearer_auth_json(monkeypatch, tmp_path):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"https://auth.x.ai::x": {"key": "tok-from-file"}})
    )
    with patch("voice_stt.AUTH_PATH", auth):
        assert resolve_bearer() == "tok-from-file"


def test_recorder_candidates_shape():
    cands = recorder_candidates(16000)
    names = [n for n, _ in cands]
    assert names == [n for n in ("pw-record", "parec", "arecord") if n in names]
    for name, args in cands:
        joined = " ".join(args)
        assert "16000" in joined or any("16000" in a for a in args)
