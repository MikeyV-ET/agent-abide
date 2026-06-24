"""Tests for irc_adapter.py — extractable functions (no network needed).

Ported from ~/projects/mikeyv-infra/tests/test_irc_adapter.py
Import path updated: adapters/irc_adapter.py (was live/comms/irc_adapter.py)
"""

import json
import os
import sys
import time
import pytest

# agent-abide layout: adapters/irc_adapter.py needs adapters/ on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'adapters'))

import irc_adapter


# ============================================================================
# clean_response
# ============================================================================

class TestCleanResponse:
    def test_normal_text(self):
        assert irc_adapter.clean_response("hello world") == "hello world"

    def test_strips_from_header(self):
        result = irc_adapter.clean_response("[FROM: eric]\nhello")
        assert result == "hello"

    def test_strips_to_header(self):
        result = irc_adapter.clean_response("[TO: Sr]\nhello")
        assert result == "hello"

    def test_strips_via_header(self):
        result = irc_adapter.clean_response("[VIA: leader-callback]\nhello")
        assert result == "hello"

    def test_strips_bold_headers(self):
        result = irc_adapter.clean_response("**[FROM: eric]**\nhello")
        assert result == "hello"

    def test_strips_multiple_headers(self):
        result = irc_adapter.clean_response("[FROM: eric]\n[TO: Sr]\n[VIA: irc]\nhello")
        assert result == "hello"

    def test_suppresses_note(self):
        assert irc_adapter.clean_response("note") is None

    def test_suppresses_noted(self):
        assert irc_adapter.clean_response("noted") is None

    def test_suppresses_note_case_insensitive(self):
        assert irc_adapter.clean_response("Note") is None
        assert irc_adapter.clean_response("NOTED") is None
        assert irc_adapter.clean_response("Noted") is None

    def test_suppresses_noted_with_period(self):
        assert irc_adapter.clean_response("Noted.") is None
        assert irc_adapter.clean_response("noted.") is None

    def test_suppresses_noted_with_punctuation(self):
        assert irc_adapter.clean_response("Noted!") is None
        assert irc_adapter.clean_response("noted;") is None
        assert irc_adapter.clean_response("Noted...") is None

    def test_does_not_suppress_note_in_sentence(self):
        result = irc_adapter.clean_response("I noted that down")
        assert result == "I noted that down"

    def test_empty_string(self):
        assert irc_adapter.clean_response("") is None

    def test_none_input(self):
        assert irc_adapter.clean_response(None) is None

    def test_whitespace_only(self):
        assert irc_adapter.clean_response("   \n  ") is None

    def test_long_messages_pass_through(self):
        """No hard cap -- chunker handles delivery at 400 chars per IRC line."""
        long = "x" * 5000
        result = irc_adapter.clean_response(long)
        assert len(result) == 5000  # full text preserved

    def test_preserves_normal_length(self):
        text = "x" * 500
        result = irc_adapter.clean_response(text)
        assert len(result) == 500

    def test_header_only_returns_none(self):
        result = irc_adapter.clean_response("[FROM: eric]\n[TO: Sr]")
        assert result is None


# ============================================================================
# parse_irc_commands
# ============================================================================

class TestParseIrcCommands:
    def test_nick_command(self):
        commands, remaining = irc_adapter.parse_irc_commands("/nick NewName")
        assert len(commands) == 1
        assert commands[0]["type"] == "nick"
        assert commands[0]["args"] == "NewName"
        assert remaining == ""

    def test_msg_command(self):
        commands, remaining = irc_adapter.parse_irc_commands("/msg eric hello there")
        assert len(commands) == 1
        assert commands[0]["type"] == "msg"
        assert commands[0]["target"] == "eric"
        assert commands[0]["text"] == "hello there"

    def test_join_command(self):
        commands, remaining = irc_adapter.parse_irc_commands("/join #new-channel")
        assert len(commands) == 1
        assert commands[0]["type"] == "join"
        assert commands[0]["args"] == "#new-channel"

    def test_part_command(self):
        commands, remaining = irc_adapter.parse_irc_commands("/part #old-channel")
        assert len(commands) == 1
        assert commands[0]["type"] == "part"
        assert commands[0]["args"] == "#old-channel"

    def test_me_command(self):
        commands, remaining = irc_adapter.parse_irc_commands("/me waves hello")
        assert len(commands) == 1
        assert commands[0]["type"] == "me"
        assert commands[0]["args"] == "waves hello"

    def test_mixed_commands_and_text(self):
        text = "/nick Trip\nHello everyone!\n/join #trip-thoughts"
        commands, remaining = irc_adapter.parse_irc_commands(text)
        assert len(commands) == 2
        assert commands[0]["type"] == "nick"
        assert commands[1]["type"] == "join"
        assert remaining == "Hello everyone!"

    def test_no_commands(self):
        commands, remaining = irc_adapter.parse_irc_commands("Just regular text")
        assert len(commands) == 0
        assert remaining == "Just regular text"

    def test_empty_input(self):
        commands, remaining = irc_adapter.parse_irc_commands("")
        assert len(commands) == 0
        assert remaining == ""

    def test_command_only_no_remaining(self):
        commands, remaining = irc_adapter.parse_irc_commands("/nick NewName")
        assert remaining == ""

    def test_preserves_multiline_text(self):
        text = "Line 1\nLine 2\nLine 3"
        commands, remaining = irc_adapter.parse_irc_commands(text)
        assert len(commands) == 0
        assert "Line 1" in remaining
        assert "Line 2" in remaining
        assert "Line 3" in remaining

    def test_msg_no_text(self):
        commands, remaining = irc_adapter.parse_irc_commands("/msg eric")
        assert len(commands) == 1
        assert commands[0]["text"] == ""

    def test_nick_empty_name_ignored(self):
        commands, remaining = irc_adapter.parse_irc_commands("/nick ")
        assert len(commands) == 0

    def test_unknown_slash_not_command(self):
        commands, remaining = irc_adapter.parse_irc_commands("/unknown something")
        assert len(commands) == 0
        assert "/unknown something" in remaining


# ============================================================================
# MessageBatcher
# ============================================================================

class TestMessageBatcher:
    def test_add_and_flush(self):
        batcher = irc_adapter.MessageBatcher(window=0.0)  # immediate
        batcher.add("Sr", {"text": "hello"})
        
        ready = batcher.ready_agents()
        assert "Sr" in ready
        
        msgs = batcher.flush("Sr")
        assert len(msgs) == 1
        assert msgs[0]["text"] == "hello"

    def test_batches_within_window(self):
        batcher = irc_adapter.MessageBatcher(window=10.0)  # long window
        batcher.add("Sr", {"text": "msg 1"})
        batcher.add("Sr", {"text": "msg 2"})
        
        # Not ready yet (window not elapsed)
        ready = batcher.ready_agents()
        assert "Sr" not in ready

    def test_per_agent_buckets(self):
        batcher = irc_adapter.MessageBatcher(window=0.0)
        batcher.add("Sr", {"text": "for Sr"})
        batcher.add("Jr", {"text": "for Jr"})
        
        sr_msgs = batcher.flush("Sr")
        jr_msgs = batcher.flush("Jr")
        assert len(sr_msgs) == 1
        assert len(jr_msgs) == 1
        assert sr_msgs[0]["text"] == "for Sr"
        assert jr_msgs[0]["text"] == "for Jr"

    def test_flush_empties_bucket(self):
        batcher = irc_adapter.MessageBatcher(window=0.0)
        batcher.add("Sr", {"text": "hello"})
        batcher.flush("Sr")
        msgs = batcher.flush("Sr")
        assert msgs == []

    def test_flush_nonexistent_agent(self):
        batcher = irc_adapter.MessageBatcher()
        msgs = batcher.flush("NonexistentAgent")
        assert msgs == []

    def test_quiet_window_resets_on_add(self):
        """Adding a new message resets the quiet timer, preventing premature flush.
        
        This ensures multi-line IRC messages (split at 400 chars) are batched
        together even if lines arrive with small gaps between them.
        """
        batcher = irc_adapter.MessageBatcher(window=1.0)
        batcher.add("Sr", {"text": "line 1"})
        # Simulate a small delay then add another line
        batcher.last_activity["Sr"] -= 0.5  # pretend 0.5s passed
        batcher.add("Sr", {"text": "line 2"})
        # Timer was reset by second add — should NOT be ready yet
        assert "Sr" not in batcher.ready_agents()
        # Now simulate the full quiet period passing
        batcher.last_activity["Sr"] -= 1.1
        assert "Sr" in batcher.ready_agents()
        msgs = batcher.flush("Sr")
        assert len(msgs) == 2


# ============================================================================
# MIKEYV_NICKS (loop suppression)
# ============================================================================

class TestNickSuppression:
    def test_all_nicks_present(self):
        assert "sr" in irc_adapter.MIKEYV_NICKS
        assert "jr" in irc_adapter.MIKEYV_NICKS
        assert "trip" in irc_adapter.MIKEYV_NICKS
        assert "q" in irc_adapter.MIKEYV_NICKS
        assert "cinco" in irc_adapter.MIKEYV_NICKS

    def test_case_insensitive(self):
        # All stored lowercase
        for nick in irc_adapter.MIKEYV_NICKS:
            assert nick == nick.lower()


# ============================================================================
# THOUGHT_CHANNELS
# ============================================================================

class TestThoughtChannels:
    def test_all_agents_have_channels(self):
        for agent in ["Sr", "Jr", "Trip", "Q", "Cinco"]:
            assert agent in irc_adapter.THOUGHT_CHANNELS
            assert irc_adapter.THOUGHT_CHANNELS[agent].startswith("#")
            assert "thoughts" in irc_adapter.THOUGHT_CHANNELS[agent]
