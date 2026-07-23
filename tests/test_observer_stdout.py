"""
Tests for BinaryStateObserver stdout event processing and InProcessObserver.

Covers:
- process_stdout_event() with sessions/changed, models/update, session_notification
- reset() preserves model state, clears turn state
- InProcessObserver state_dict() includes new fields
- state_dict() serialization of stdout-derived fields
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))

from binary_state_observer import BinaryStateObserver, ObserverState


# ── Helpers ──────────────────────────────────────────────────────────────

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'core', 'observer_data')


def make_observer(**kwargs):
    """Create a BinaryStateObserver with known_types loaded from data."""
    from binary_state_observer import load_known_types, load_silence_windows
    known = load_known_types(DATA_DIR)
    tw, ew, dw, _ = load_silence_windows(DATA_DIR)
    return BinaryStateObserver(
        pid=kwargs.get('pid', 99999),
        known_types=known,
        silence_windows=tw,
        event_silence_windows=ew,
        **{k: v for k, v in kwargs.items() if k != 'pid'},
    )


# ── process_stdout_event: sessions/changed ──────────────────────────────

class TestSessionsChanged:
    def test_model_changed_notification(self):
        obs = make_observer()
        assert obs.model_id == "unknown"
        assert obs.reasoning_effort is None

        obs.process_stdout_event({
            "jsonrpc": "2.0",
            "method": "_x.ai/session_notification",
            "params": {
                "update": {
                    "sessionUpdate": "model_changed",
                    "model_id": "grok-4.5",
                    "reasoning_effort": "xhigh",
                }
            }
        })

        assert obs.model_id == "grok-4.5"
        assert obs.reasoning_effort == "xhigh"

    def test_session_changed_with_activity(self):
        obs = make_observer()
        assert obs.activity is None

        obs.process_stdout_event({
            "jsonrpc": "2.0",
            "method": "_x.ai/session_notification",
            "params": {
                "update": {
                    "sessionUpdate": "session_changed",
                    "activity": "idle",
                    "model_id": "sxs-claude-opus-4-6",
                }
            }
        })

        assert obs.activity == "idle"
        assert obs.model_id == "sxs-claude-opus-4-6"

    def test_sessions_changed_direct(self):
        """Test the direct _x.ai/sessions/changed method (alternative format)."""
        obs = make_observer()

        obs.process_stdout_event({
            "jsonrpc": "2.0",
            "method": "_x.ai/sessions/changed",
            "params": {
                "model_id": "grok-4.3",
                "reasoning_effort": "high",
                "activity": "busy",
            }
        })

        assert obs.model_id == "grok-4.3"
        assert obs.reasoning_effort == "high"
        assert obs.activity == "busy"

    def test_partial_update_preserves_existing(self):
        obs = make_observer()
        # Set initial state
        obs.process_stdout_event({
            "jsonrpc": "2.0",
            "method": "_x.ai/sessions/changed",
            "params": {
                "model_id": "grok-4.5",
                "reasoning_effort": "xhigh",
                "activity": "busy",
            }
        })

        # Partial update — only activity changes
        obs.process_stdout_event({
            "jsonrpc": "2.0",
            "method": "_x.ai/sessions/changed",
            "params": {
                "activity": "idle",
            }
        })

        assert obs.model_id == "grok-4.5"       # preserved
        assert obs.reasoning_effort == "xhigh"   # preserved
        assert obs.activity == "idle"             # updated


class TestModelsUpdate:
    def test_models_update(self):
        obs = make_observer()
        assert obs.available_models is None

        models = [
            {"id": "grok-4.3", "name": "Grok 4.3"},
            {"id": "grok-4.5", "name": "Grok 4.5"},
        ]

        obs.process_stdout_event({
            "jsonrpc": "2.0",
            "method": "_x.ai/models/update",
            "params": {"models": models}
        })

        assert obs.available_models == models

    def test_unrelated_method_ignored(self):
        obs = make_observer()
        obs.process_stdout_event({
            "jsonrpc": "2.0",
            "method": "session/request_permission",
            "params": {"toolCall": {"kind": "bash"}}
        })

        # Nothing should change
        assert obs.model_id == "unknown"
        assert obs.reasoning_effort is None
        assert obs.activity is None


# ── reset() ──────────────────────────────────────────────────────────────

class TestReset:
    def test_reset_preserves_model_state(self):
        obs = make_observer(pid=1000)
        obs.process_stdout_event({
            "jsonrpc": "2.0",
            "method": "_x.ai/sessions/changed",
            "params": {"model_id": "grok-4.5", "reasoning_effort": "xhigh"}
        })

        # Simulate some turn activity
        obs.process_event({
            "jsonrpc": "2.0",
            "method": "_x.ai/session_notification",
            "params": {"update": {"sessionUpdate": "user_message_chunk"}}
        })
        assert obs.state == ObserverState.BUSY

        # Reset for new PID
        obs.reset(2000)

        assert obs.pid == 2000
        assert obs.state == ObserverState.STARTING
        assert obs.model_id == "grok-4.5"         # preserved
        assert obs.reasoning_effort == "xhigh"     # preserved
        assert obs.turn_event_count == 0           # cleared
        assert not obs.has_pending_tool_calls      # cleared
        assert obs.doom_loop is False              # cleared

    def test_reset_clears_gone_metadata(self):
        obs = make_observer(pid=1000)
        obs._exit_code = 1
        obs._pid_proc_state = "Z"
        obs._set_state(ObserverState.GONE)

        obs.reset(2000)

        assert obs.state == ObserverState.STARTING
        assert obs._exit_code is None
        assert obs._pid_proc_state is None


# ── state_dict includes new fields ───────────────────────────────────────

class TestStateDictNewFields:
    def test_state_dict_includes_stdout_fields(self):
        obs = make_observer()
        obs.process_stdout_event({
            "jsonrpc": "2.0",
            "method": "_x.ai/sessions/changed",
            "params": {
                "model_id": "grok-4.5",
                "reasoning_effort": "high",
                "activity": "busy",
            }
        })

        d = obs.state_dict()
        assert d["model_id"] == "grok-4.5"
        assert d["reasoning_effort"] == "high"
        assert d["activity"] == "busy"

    def test_state_dict_defaults(self):
        obs = make_observer()
        d = obs.state_dict()
        assert d["model_id"] == "unknown"
        assert d["reasoning_effort"] is None
        assert d["activity"] is None


# ── stdout events don't interfere with updates.jsonl events ──────────────

class TestNoInterference:
    def test_stdout_doesnt_change_turn_state(self):
        obs = make_observer()
        # Start a turn
        obs.process_event({
            "jsonrpc": "2.0",
            "method": "_x.ai/session_notification",
            "params": {"update": {"sessionUpdate": "user_message_chunk"}}
        })
        assert obs.state == ObserverState.BUSY

        # Stdout event arrives mid-turn
        obs.process_stdout_event({
            "jsonrpc": "2.0",
            "method": "_x.ai/sessions/changed",
            "params": {"activity": "busy", "model_id": "grok-4.5"}
        })

        # Turn state unchanged
        assert obs.state == ObserverState.BUSY
        # But model_id updated
        assert obs.model_id == "grok-4.5"

    def test_interleaved_events(self):
        obs = make_observer()

        # Effort change via stdout
        obs.process_stdout_event({
            "jsonrpc": "2.0",
            "method": "_x.ai/session_notification",
            "params": {"update": {"sessionUpdate": "model_changed", "reasoning_effort": "xhigh", "model_id": "grok-4.5"}}
        })
        assert obs.reasoning_effort == "xhigh"

        # Turn starts via updates.jsonl
        obs.process_event({
            "jsonrpc": "2.0",
            "method": "_x.ai/session_notification",
            "params": {"update": {"sessionUpdate": "user_message_chunk"}}
        })
        assert obs.state == ObserverState.BUSY
        assert obs.reasoning_effort == "xhigh"  # still there

        # Turn ends
        obs.process_event({
            "jsonrpc": "2.0",
            "method": "_x.ai/session_notification",
            "params": {"update": {"sessionUpdate": "turn_completed"}}
        })
        assert obs.state == ObserverState.IDLE
        assert obs.reasoning_effort == "xhigh"  # still there
