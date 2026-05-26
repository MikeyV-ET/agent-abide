"""
Tests for the asdaaas HTTP API: normalizers, session_locator, server endpoints.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Add api/ to path
sys.path.insert(0, str(Path(__file__).parent.parent / "api"))

from normalizers import parse_grok_line, parse_claude_line, read_messages, FileTailer


# ---------------------------------------------------------------------------
# Grok normalizer tests
# ---------------------------------------------------------------------------

class TestGrokNormalizer:
    def test_user_message(self):
        line = json.dumps({
            "timestamp": 1700000000,
            "method": "session/update",
            "params": {"update": {
                "sessionUpdate": "user_message_chunk",
                "content": {"type": "text", "text": "hello"}
            }}
        })
        msg = parse_grok_line(line)
        assert msg is not None
        assert msg["role"] == "user"
        assert msg["content"] == "hello"
        assert msg["raw_type"] == "user_message_chunk"

    def test_assistant_message(self):
        line = json.dumps({
            "timestamp": 1700000001,
            "method": "session/update",
            "params": {"update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "world"}
            }}
        })
        msg = parse_grok_line(line)
        assert msg["role"] == "assistant"
        assert msg["content"] == "world"

    def test_thinking(self):
        line = json.dumps({
            "timestamp": 1700000002,
            "method": "session/update",
            "params": {"update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "let me think..."}
            }}
        })
        msg = parse_grok_line(line)
        assert msg["role"] == "thinking"

    def test_tool_call(self):
        line = json.dumps({
            "timestamp": 1700000003,
            "method": "session/update",
            "params": {"update": {
                "sessionUpdate": "tool_call",
                "toolCallName": "read_file",
                "content": {"type": "text", "text": "/etc/hosts"}
            }}
        })
        msg = parse_grok_line(line)
        assert msg["role"] == "tool_call"
        assert "[read_file]" in msg["content"]

    def test_tool_result_list_content(self):
        line = json.dumps({
            "timestamp": 1700000004,
            "method": "session/update",
            "params": {"update": {
                "sessionUpdate": "tool_call_update",
                "content": [
                    {"type": "content", "content": {"type": "text", "text": "file contents here"}}
                ]
            }}
        })
        msg = parse_grok_line(line)
        assert msg["role"] == "tool_result"
        assert msg["content"] == "file contents here"

    def test_skipped_types(self):
        for skip_type in ["available_commands_update", "plan", "retry_state",
                          "compaction_checkpoint", "hook_execution"]:
            line = json.dumps({
                "timestamp": 1700000000,
                "method": "session/update",
                "params": {"update": {"sessionUpdate": skip_type}}
            })
            assert parse_grok_line(line) is None

    def test_invalid_json(self):
        assert parse_grok_line("not json{{{") is None

    def test_empty_content(self):
        line = json.dumps({
            "timestamp": 1700000000,
            "method": "session/update",
            "params": {"update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": ""}
            }}
        })
        msg = parse_grok_line(line)
        assert msg is not None
        assert msg["content"] == ""


# ---------------------------------------------------------------------------
# Claude normalizer tests
# ---------------------------------------------------------------------------

class TestClaudeNormalizer:
    def test_user_message(self):
        line = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "hello"},
            "uuid": "abc-123",
            "timestamp": "2026-05-25T10:00:00Z"
        })
        msg = parse_claude_line(line)
        assert msg["role"] == "user"
        assert msg["content"] == "hello"
        assert msg["timestamp"] == "2026-05-25T10:00:00Z"

    def test_assistant_with_text_blocks(self):
        line = json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Here is the answer"},
                {"type": "text", "text": "and more"}
            ]},
            "timestamp": "2026-05-25T10:00:01Z"
        })
        msg = parse_claude_line(line)
        assert msg["role"] == "assistant"
        assert "Here is the answer" in msg["content"]
        assert "and more" in msg["content"]

    def test_assistant_with_tool_use(self):
        line = json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Let me check"},
                {"type": "tool_use", "name": "Read"}
            ]},
            "timestamp": "2026-05-25T10:00:02Z"
        })
        msg = parse_claude_line(line)
        assert "[tool: Read]" in msg["content"]

    def test_skipped_types(self):
        for skip_type in ["queue-operation", "attachment", "ai-title", "last-prompt"]:
            line = json.dumps({"type": skip_type, "timestamp": "2026-05-25T10:00:00Z"})
            assert parse_claude_line(line) is None

    def test_string_content(self):
        line = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "plain string"},
            "timestamp": "2026-05-25T10:00:00Z"
        })
        msg = parse_claude_line(line)
        assert msg["content"] == "plain string"

    def test_invalid_json(self):
        assert parse_claude_line("{broken") is None


# ---------------------------------------------------------------------------
# read_messages tests
# ---------------------------------------------------------------------------

class TestReadMessages:
    def _write_grok_session(self, tmpdir, messages):
        p = tmpdir / "session.jsonl"
        with open(p, "w") as f:
            for i, (role_type, text) in enumerate(messages):
                obj = {
                    "timestamp": 1700000000 + i,
                    "method": "session/update",
                    "params": {"update": {
                        "sessionUpdate": role_type,
                        "content": {"type": "text", "text": text}
                    }}
                }
                f.write(json.dumps(obj) + "\n")
        return p

    def test_last_n(self, tmp_path):
        p = self._write_grok_session(tmp_path, [
            ("user_message_chunk", "msg1"),
            ("agent_message_chunk", "msg2"),
            ("user_message_chunk", "msg3"),
            ("agent_message_chunk", "msg4"),
        ])
        msgs = read_messages(p, "grok", last=2)
        assert len(msgs) == 2
        assert msgs[0]["content"] == "msg3"
        assert msgs[1]["content"] == "msg4"

    def test_before_and_limit(self, tmp_path):
        p = self._write_grok_session(tmp_path, [
            ("user_message_chunk", f"msg{i}") for i in range(10)
        ])
        msgs = read_messages(p, "grok", before=5, limit=2)
        assert len(msgs) == 2
        assert msgs[0]["id"] == 3
        assert msgs[1]["id"] == 4

    def test_sequential_ids(self, tmp_path):
        p = self._write_grok_session(tmp_path, [
            ("user_message_chunk", "a"),
            ("available_commands_update", "skip"),  # not a known role
            ("agent_message_chunk", "b"),
        ])
        msgs = read_messages(p, "grok")
        assert len(msgs) == 2
        assert msgs[0]["id"] == 0
        assert msgs[1]["id"] == 1


# ---------------------------------------------------------------------------
# FileTailer tests
# ---------------------------------------------------------------------------

class TestFileTailer:
    def test_poll_new_messages(self, tmp_path):
        p = tmp_path / "session.jsonl"
        with open(p, "w") as f:
            f.write(json.dumps({
                "timestamp": 1700000000,
                "method": "session/update",
                "params": {"update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "first"}
                }}
            }) + "\n")

        tailer = FileTailer(p, "grok", start_id=-1)
        # First poll gets existing message
        msgs = tailer.poll()
        assert len(msgs) == 0  # start_id=-1 means start from end

    def test_poll_after_append(self, tmp_path):
        p = tmp_path / "session.jsonl"
        with open(p, "w") as f:
            f.write(json.dumps({
                "timestamp": 1700000000,
                "method": "session/update",
                "params": {"update": {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "first"}
                }}
            }) + "\n")

        tailer = FileTailer(p, "grok", start_id=-1)
        tailer.poll()  # consume existing

        # Append new message
        with open(p, "a") as f:
            f.write(json.dumps({
                "timestamp": 1700000001,
                "method": "session/update",
                "params": {"update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "second"}
                }}
            }) + "\n")

        msgs = tailer.poll()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "second"
        assert msgs[0]["role"] == "assistant"

    def test_raw_mode(self, tmp_path):
        p = tmp_path / "session.jsonl"
        with open(p, "w") as f:
            f.write(json.dumps({
                "timestamp": 1700000000,
                "method": "session/update",
                "params": {"update": {
                    "sessionUpdate": "available_commands_update",
                    "commands": []
                }}
            }) + "\n")

        tailer = FileTailer(p, "grok", start_id=-2)
        # In raw mode, ALL lines come through (including ones normalizer skips)
        tailer.byte_offset = 0
        tailer.next_id = 0
        msgs = tailer.poll(raw=True)
        assert len(msgs) == 1
        assert "_tail_id" in msgs[0]
        assert msgs[0]["params"]["update"]["sessionUpdate"] == "available_commands_update"

    def test_poll_from_specific_id(self, tmp_path):
        p = tmp_path / "session.jsonl"
        with open(p, "w") as f:
            for i in range(5):
                f.write(json.dumps({
                    "timestamp": 1700000000 + i,
                    "method": "session/update",
                    "params": {"update": {
                        "sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": f"msg{i}"}
                    }}
                }) + "\n")

        tailer = FileTailer(p, "grok", start_id=2)
        msgs = tailer.poll()
        assert len(msgs) == 2
        assert msgs[0]["content"] == "msg3"
        assert msgs[1]["content"] == "msg4"

    def test_missing_file(self, tmp_path):
        p = tmp_path / "nonexistent.jsonl"
        tailer = FileTailer(p, "grok", start_id=-1)
        msgs = tailer.poll()
        assert msgs == []


# ---------------------------------------------------------------------------
# SessionLocator tests
# ---------------------------------------------------------------------------

from session_locator import SessionLocator

class TestSessionLocator:
    def _make_config(self, tmp_path):
        cfg = {
            "settings": {"asdaaas_dir": str(tmp_path)},
            "agents": {
                "TestGrok": {
                    "home": "/home/test/agents/TestGrok",
                    "session": "abc-123",
                    "backend": "grok"
                },
                "TestClaude": {
                    "home": "/home/test/agents/TestClaude",
                    "session": "def-456",
                    "backend": "claude"
                },
                "NoSession": {
                    "home": "/home/test/agents/NoSession",
                    "session": ""
                }
            }
        }
        p = tmp_path / "agents.json"
        with open(p, "w") as f:
            json.dump(cfg, f)
        return p

    def test_list_agents(self, tmp_path):
        cfg = self._make_config(tmp_path)
        loc = SessionLocator(config_path=cfg)
        agents = loc.list_agents()
        assert len(agents) == 3
        names = {a["name"] for a in agents}
        assert names == {"TestGrok", "TestClaude", "NoSession"}

    def test_backend_detection(self, tmp_path):
        cfg = self._make_config(tmp_path)
        loc = SessionLocator(config_path=cfg)
        assert loc.agent_backend("TestGrok") == "grok"
        assert loc.agent_backend("TestClaude") == "claude"
        assert loc.agent_backend("NoSession") == "grok"  # default

    def test_grok_session_path(self, tmp_path):
        cfg = self._make_config(tmp_path)
        loc = SessionLocator(config_path=cfg)
        p = loc.session_file("TestGrok")
        assert p is not None
        assert "abc-123" in str(p)
        assert "updates.jsonl" in str(p)

    def test_claude_session_path(self, tmp_path):
        cfg = self._make_config(tmp_path)
        loc = SessionLocator(config_path=cfg)
        p = loc.session_file("TestClaude")
        assert p is not None
        assert "def-456" in str(p)
        assert str(p).endswith(".jsonl")

    def test_no_session(self, tmp_path):
        cfg = self._make_config(tmp_path)
        loc = SessionLocator(config_path=cfg)
        assert loc.session_file("NoSession") is None

    def test_unknown_agent(self, tmp_path):
        cfg = self._make_config(tmp_path)
        loc = SessionLocator(config_path=cfg)
        assert loc.agent_config("Ghost") is None
        assert loc.session_file("Ghost") is None
