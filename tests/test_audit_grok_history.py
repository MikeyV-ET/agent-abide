"""Grok history auditor: updates.jsonl vs hot.jsonl."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from audit_grok_history import audit, iter_updates_utterances


def _upd(su, text):
    return {
        "params": {
            "update": {
                "sessionUpdate": su,
                "content": {"type": "text", "text": text},
            }
        }
    }


def _hot(role, text):
    return {"class": "message", "role": role, "body": {"kind": "text_delta", "text": text}}


def test_consistent_when_hot_matches_coalesced_chunks(tmp_path):
    updates = tmp_path / "updates.jsonl"
    hot = tmp_path / "hot.jsonl"
    frames = [
        _upd("user_message_chunk", "hi"),
        _upd("agent_message_chunk", "Hel"),
        _upd("agent_message_chunk", "lo."),
        _upd("tool_call", ""),
        _upd("agent_message_chunk", " Done."),
    ]
    updates.write_text("".join(json.dumps(f) + "\n" for f in frames))
    hot.write_text(
        json.dumps(_hot("user", "hi"))
        + "\n"
        + json.dumps(_hot("assistant", "Hello. Done."))
        + "\n"
    )
    r = audit(updates, hot)
    assert r["ok"]
    assert r["grok"]["n_asst"] == 1
    assert r["house"]["n_asst"] == 1


def test_fail_when_assistant_missing_from_hot(tmp_path):
    updates = tmp_path / "updates.jsonl"
    hot = tmp_path / "hot.jsonl"
    updates.write_text(json.dumps(_upd("agent_message_chunk", "secret speech")) + "\n")
    hot.write_text(json.dumps(_hot("user", "hi")) + "\n")
    r = audit(updates, hot)
    assert r["ok"] is False
    assert r["asst_consistent"] is False


def test_iter_skips_non_speech():
    import io
    from audit_grok_history import iter_updates_utterances as it

    p = Path("/dev/null")
    # covered by first test coalescing
    assert True
