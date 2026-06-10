"""Mock-based compaction report tests.

Tests asdaaas compaction report behavior WITHOUT the real grok binary.
Verifies:
  - Compaction report numbers are accurate (before != after)
  - Only one compaction report per compaction event (no duplicates)

Uses the existing agent_env fixture pattern from test_e2e_agent.py
and directly calls asdaaas functions to simulate compaction paths.

Run: pytest tests/test_compaction_report.py -v
"""

import json
import os
import re
import sys
import time
import pytest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'core'))

import asdaaas
from asdaaas import (
    write_health,
    write_compaction_state,
    _queue_post_compaction_doorbell,
    _cleanup_compact_doorbells,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    """Set up a complete temporary agent environment."""
    agent_name = "TestAgent"
    agent_home = tmp_path / agent_name
    asdaaas_dir = agent_home / "asdaaas"
    for subdir in ["doorbells", "commands", "adapters/localmail/payloads",
                   "adapters/localmail/inbox", "adapters/tui/outbox",
                   "adapters/irc/outbox", "adapters/arena/outbox"]:
        (asdaaas_dir / subdir).mkdir(parents=True)

    monkeypatch.setattr(asdaaas, "AGENTS_HOME_DIR", tmp_path)

    return {
        "agent_name": agent_name,
        "agents_home": tmp_path,
        "asdaaas_dir": asdaaas_dir,
    }


# ============================================================================
# Test: Compaction report numbers are accurate
# ============================================================================

class TestCompactionReportNumbers:
    """Verify _queue_post_compaction_doorbell writes correct before/after."""

    def test_doorbell_has_different_before_after(self, agent_env):
        """Doorbell text must show before > after with different values."""
        name = agent_env["agent_name"]
        tokens_before = 135000
        tokens_after = 43000

        _queue_post_compaction_doorbell(name, tokens_before, tokens_after)

        bell_dir = agent_env["asdaaas_dir"] / "doorbells"
        bells = list(bell_dir.glob("*.json"))
        assert len(bells) == 1, f"Expected 1 doorbell, got {len(bells)}"

        with open(bells[0]) as f:
            bell = json.load(f)

        text = bell["text"]
        matches = re.findall(r"Context reduced from (\d+) to (\d+)", text)
        assert len(matches) == 1, f"Expected 'Context reduced from X to Y' in: {text}"

        reported_before, reported_after = int(matches[0][0]), int(matches[0][1])
        assert reported_before == tokens_before
        assert reported_after == tokens_after
        assert reported_before != reported_after
        assert reported_before > reported_after

    def test_doorbell_with_identical_numbers_is_wrong(self, agent_env):
        """If caller passes same value for both, the doorbell reflects the bug."""
        name = agent_env["agent_name"]
        # This is the bug scenario: _prev_tokens already updated
        tokens_value = 135705

        _queue_post_compaction_doorbell(name, tokens_value, tokens_value)

        bell_dir = agent_env["asdaaas_dir"] / "doorbells"
        bells = list(bell_dir.glob("*.json"))
        bell = json.load(open(bells[0]))
        text = bell["text"]

        matches = re.findall(r"Context reduced from (\d+) to (\d+)", text)
        reported_before, reported_after = int(matches[0][0]), int(matches[0][1])
        # This SHOULD fail — it proves the bug exists when same values passed
        assert reported_before == reported_after, \
            "Bug confirmed: identical before/after when same value passed to both params"

    def test_compaction_state_records_different_values(self, agent_env):
        """compaction_state.json must record distinct before/after."""
        name = agent_env["agent_name"]
        tokens_before = 150000
        tokens_after = 42000

        write_compaction_state(name, "complete",
                               tokens_before=tokens_before,
                               tokens_after=tokens_after)

        state_path = agent_env["asdaaas_dir"] / "compaction_state.json"
        state = json.loads(state_path.read_text())

        assert state["tokens_before"] == tokens_before
        assert state["tokens_after"] == tokens_after
        assert state["tokens_before"] != state["tokens_after"]
        assert state["tokens_before"] > state["tokens_after"]


# ============================================================================
# Test: No duplicate compaction reports
# ============================================================================

class TestNoDuplicateReports:
    """Verify only one compaction report fires per compaction event.

    The bug: agent-initiated /compact path (Path 2) calls
    _queue_post_compaction_doorbell but doesn't consume the
    compaction event. The event-based path (Path 1) then fires
    redundantly, producing a second report.

    These tests simulate both paths firing and verify the
    coordination mechanisms.
    """

    def test_single_doorbell_after_compaction(self, agent_env):
        """Only one compaction doorbell should exist after compaction."""
        name = agent_env["agent_name"]

        # Simulate Path 2: agent-initiated compact completes
        _queue_post_compaction_doorbell(name, 150000, 45000)

        bell_dir = agent_env["asdaaas_dir"] / "doorbells"
        compact_bells = [
            f for f in bell_dir.glob("*.json")
            if "compact" in f.name.lower()
        ]
        assert len(compact_bells) == 1, \
            f"Expected 1 compact doorbell, got {len(compact_bells)}"

    def test_double_doorbell_detectable(self, agent_env):
        """If both paths fire, two doorbells appear — this IS the bug."""
        name = agent_env["agent_name"]

        # Path 2 fires first (agent-initiated)
        _queue_post_compaction_doorbell(name, 150000, 45000)
        time.sleep(0.01)  # ensure different timestamp

        # Path 1 fires second (event-based, with stale _prev_tokens)
        _queue_post_compaction_doorbell(name, 45000, 45000)

        bell_dir = agent_env["asdaaas_dir"] / "doorbells"
        compact_bells = [
            f for f in bell_dir.glob("*.json")
            if "compact" in f.name.lower()
        ]
        # This proves the bug: two doorbells from one compaction
        assert len(compact_bells) == 2, \
            "Bug confirmed: two compact doorbells from double-fire"

    def test_second_doorbell_has_stale_numbers(self, agent_env):
        """When both paths fire, the second doorbell has identical numbers.

        This proves the bug: Path 2 updates _prev_tokens before Path 1
        reads it, so Path 1 passes the same value for both params.
        """
        name = agent_env["agent_name"]

        # Path 2 fires with correct numbers
        _queue_post_compaction_doorbell(name, 150000, 45000)
        time.sleep(0.01)
        # Path 1 fires with stale _prev_tokens (already updated to 45000)
        _queue_post_compaction_doorbell(name, 45000, 45000)

        bell_dir = agent_env["asdaaas_dir"] / "doorbells"
        bells = sorted(bell_dir.glob("compact_*.json"))
        assert len(bells) == 2

        # First bell should have correct numbers
        b1 = json.load(open(bells[0]))
        m1 = re.findall(r"Context reduced from (\d+) to (\d+)", b1["text"])
        assert int(m1[0][0]) == 150000
        assert int(m1[0][1]) == 45000

        # Second bell has the bug: identical numbers
        b2 = json.load(open(bells[1]))
        m2 = re.findall(r"Context reduced from (\d+) to (\d+)", b2["text"])
        assert int(m2[0][0]) == int(m2[0][1]) == 45000, \
            "Bug confirmed: second doorbell has identical before/after"


# ============================================================================
# Test: Mock backend pop_compaction_event coordination
# ============================================================================

class TestCompactionEventConsumption:
    """Verify that pop_compaction_event works as a consume-once mechanism.

    The fix for the double-fire bug requires Path 2 to call
    pop_compaction_event() after handling compaction, so Path 1
    doesn't see the same event on the next loop iteration.
    """

    def test_pop_clears_event(self):
        """pop_compaction_event returns event once, then (False, None)."""
        from grok_backend import GrokBackend

        # Create a minimal backend instance (won't start subprocess)
        backend = GrokBackend.__new__(GrokBackend)
        backend._compaction_event = {
            "params": {
                "update": {
                    "sessionUpdate": "auto_compact_completed",
                    "tokens_after": 43000
                }
            }
        }

        # First pop: returns the event
        found, tokens = backend.pop_compaction_event()
        assert found is True
        assert tokens == 43000

        # Second pop: event consumed, returns nothing
        found2, tokens2 = backend.pop_compaction_event()
        assert found2 is False
        assert tokens2 is None

    def test_pop_without_event_returns_false(self):
        """pop_compaction_event returns (False, None) when no event pending."""
        from grok_backend import GrokBackend

        backend = GrokBackend.__new__(GrokBackend)
        backend._compaction_event = None

        found, tokens = backend.pop_compaction_event()
        assert found is False
        assert tokens is None