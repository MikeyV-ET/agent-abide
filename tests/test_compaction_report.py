"""Mock-based compaction report tests.

Tests asdaaas compaction report behavior WITHOUT the real grok binary.

Two test levels:
  1. Component tests: directly call asdaaas functions to prove bug mechanics
  2. Integration tests: mock backend with real updates.jsonl to simulate
     the full compaction flow as asdaaas would see it

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
from grok_backend import GrokBackend, FileEventSource


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
        backend._compaction_tokens_before = 150000
        backend._compaction_tokens_after = 43000

        # First pop: returns the event
        found, tokens, tokens_before = backend.pop_compaction_event()
        assert found is True
        assert tokens == 43000

        # Second pop: event consumed, returns nothing
        found2, tokens2, _ = backend.pop_compaction_event()
        assert found2 is False
        assert tokens2 is None

    def test_pop_without_event_returns_false(self):
        """pop_compaction_event returns (False, None, 0) when no event pending."""
        from grok_backend import GrokBackend

        backend = GrokBackend.__new__(GrokBackend)
        backend._compaction_event = None
        backend._compaction_tokens_before = 0
        backend._compaction_tokens_after = 0

        found, tokens, tokens_before = backend.pop_compaction_event()
        assert found is False
        assert tokens is None


# ============================================================================
# Integration tests: mock backend with real updates.jsonl
# ============================================================================

@pytest.fixture
def mock_session(tmp_path):
    """Create a mock session directory with updates.jsonl.

    Returns a dict with the session dir, updates.jsonl path, and a
    helper to write events to the file (simulating what grok does).
    """
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    updates_path = session_dir / "updates.jsonl"
    updates_path.touch()

    def write_event(event_dict):
        """Append a JSON event to updates.jsonl (simulates grok binary)."""
        with open(updates_path, "a") as f:
            f.write(json.dumps(event_dict) + "\n")

    def write_meta(total_tokens):
        """Write a _meta frame with token count."""
        write_event({
            "method": "_x.ai/session/update",
            "params": {
                "_meta": {"totalTokens": total_tokens}
            }
        })

    def write_compaction_complete(tokens_after):
        """Write auto_compact_completed event (what grok emits after compaction)."""
        write_event({
            "method": "_x.ai/session/update",
            "params": {
                "update": {
                    "sessionUpdate": "auto_compact_completed",
                    "tokens_after": tokens_after
                }
            }
        })

    return {
        "session_dir": session_dir,
        "updates_path": updates_path,
        "write_event": write_event,
        "write_meta": write_meta,
        "write_compaction_complete": write_compaction_complete,
    }


class TestDoubleFireIntegration:
    """Integration test: simulates the full double-fire scenario.

    Uses a real FileEventSource reading from a real updates.jsonl file,
    but no grok binary. Simulates what happens when:
    1. Agent sends /compact command
    2. grok binary compacts (token drop + auto_compact_completed event)
    3. asdaaas Path 2 detects the drop via refresh_tokens() polling
    4. asdaaas Path 1 detects the event via pop_compaction_event()

    The bug: both paths fire for the same compaction.
    """

    def _make_backend_with_source(self, session_dir):
        """Create a GrokBackend wired to a real FileEventSource."""
        backend = GrokBackend.__new__(GrokBackend)
        backend._total_tokens = 0
        backend._compaction_event = None
        backend._compaction_tokens_before = 0
        backend._compaction_tokens_after = 0
        backend._last_activity_ts = 0.0
        backend._file_source = FileEventSource(session_dir)
        # Open and seek to start (we want to read everything)
        backend._file_source._updates_path = session_dir / "updates.jsonl"
        backend._file_source._updates_fp = open(session_dir / "updates.jsonl", "r")
        backend._file_source._events_path = session_dir / "events.jsonl"
        # Create events.jsonl so FileEventSource doesn't error
        (session_dir / "events.jsonl").touch()
        backend._file_source._events_fp = open(session_dir / "events.jsonl", "r")
        return backend

    def test_refresh_tokens_picks_up_compaction_event(self, mock_session):
        """refresh_tokens() reads auto_compact_completed and stores it."""
        backend = self._make_backend_with_source(mock_session["session_dir"])

        # Simulate pre-compaction: agent has 150k tokens
        mock_session["write_meta"](150000)
        tokens = backend.refresh_tokens()
        assert tokens == 150000

        # Simulate compaction: grok writes auto_compact_completed
        mock_session["write_compaction_complete"](43000)
        tokens = backend.refresh_tokens()
        assert tokens == 43000

        # Event should be stored for pop_compaction_event
        found, event_tokens, _ = backend.pop_compaction_event()
        assert found is True
        assert event_tokens == 43000

    def test_double_fire_scenario(self, mock_session, agent_env):
        """Reproduce the exact double-fire bug.

        Simulates:
        1. Pre-compaction: 150k tokens
        2. /compact sent, grok reduces tokens + writes event
        3. Path 2 polls refresh_tokens(), sees drop, queues doorbell
        4. Path 2 does NOT pop the event (the bug)
        5. Path 1 reads pop_compaction_event() → fires again
        """
        name = agent_env["agent_name"]
        backend = self._make_backend_with_source(mock_session["session_dir"])

        # --- Pre-compaction state ---
        mock_session["write_meta"](150000)
        backend.refresh_tokens()
        _prev_tokens = backend.total_tokens
        assert _prev_tokens == 150000

        # --- Simulate grok compaction ---
        # Binary writes reduced _meta + auto_compact_completed
        mock_session["write_meta"](43000)
        mock_session["write_compaction_complete"](43000)

        # --- Path 2: agent-initiated /compact polling ---
        # refresh_tokens() picks up both the _meta and the event
        tokens_before = _prev_tokens  # captured before compaction
        total_tokens = backend.refresh_tokens()
        assert total_tokens == 43000

        # Path 2 detects the drop
        assert total_tokens < tokens_before * 0.6

        # Path 2 queues doorbell with CORRECT numbers
        _queue_post_compaction_doorbell(name, tokens_before, total_tokens)

        # Path 2 updates state (this is what causes the bug)
        _prev_tokens = total_tokens  # now 43000
        turns_since_compaction = 0

        # --- Simulate next main loop iteration ---
        # One turn happens (doorbell delivered, agent responds)
        turns_since_compaction = 1

        # Path 1: event-based detection at top of loop
        total_tokens = backend.refresh_tokens()  # no new data
        compaction_event, event_tokens, _ = backend.pop_compaction_event()

        # THE BUG: event was never consumed by Path 2
        # So pop_compaction_event() returns it again
        bug_present = compaction_event is True

        if bug_present:
            # Path 1 fires with stale _prev_tokens
            path1_tokens_before = _prev_tokens  # 43000 (post-compaction!)
            path1_tokens_after = event_tokens or total_tokens  # 43000
            assert path1_tokens_before == path1_tokens_after, \
                "Bug confirmed: Path 1 sees identical before/after"

            # Path 1 would queue SECOND doorbell/orientation
            _queue_post_compaction_doorbell(name, path1_tokens_before, path1_tokens_after)

        # Count doorbells — should be 1, but bug produces 2
        bell_dir = agent_env["asdaaas_dir"] / "doorbells"
        compact_bells = list(bell_dir.glob("compact_*.json"))
        assert len(compact_bells) == 2, \
            f"Bug confirmed: expected 2 doorbells (double-fire), got {len(compact_bells)}"

        # Verify we have one correct and one stale doorbell (order-independent)
        all_numbers = []
        for f in compact_bells:
            text = json.load(open(f))["text"]
            m = re.findall(r"from (\d+) to (\d+)", text)
            all_numbers.append((int(m[0][0]), int(m[0][1])))

        correct = (150000, 43000)
        stale = (43000, 43000)
        assert correct in all_numbers, f"Missing correct doorbell {correct} in {all_numbers}"
        assert stale in all_numbers, f"Missing stale doorbell {stale} in {all_numbers}"

    def test_fix_pop_prevents_double_fire(self, mock_session, agent_env):
        """Verify the fix: calling pop_compaction_event() in Path 2.

        Same scenario as above, but Path 2 consumes the event.
        Path 1 should NOT fire.
        """
        name = agent_env["agent_name"]
        backend = self._make_backend_with_source(mock_session["session_dir"])

        # Pre-compaction
        mock_session["write_meta"](150000)
        backend.refresh_tokens()
        _prev_tokens = backend.total_tokens

        # Simulate compaction
        mock_session["write_meta"](43000)
        mock_session["write_compaction_complete"](43000)

        # --- Path 2 with fix applied ---
        tokens_before = _prev_tokens
        total_tokens = backend.refresh_tokens()

        # Path 2 queues doorbell
        _queue_post_compaction_doorbell(name, tokens_before, total_tokens)

        # THE FIX: Path 2 consumes the event
        backend.pop_compaction_event()

        _prev_tokens = total_tokens
        turns_since_compaction = 0

        # --- Next loop iteration ---
        turns_since_compaction = 1
        total_tokens = backend.refresh_tokens()
        compaction_event, event_tokens, _ = backend.pop_compaction_event()

        # Event was consumed — Path 1 does NOT fire
        assert compaction_event is False
        assert event_tokens is None

        # Only ONE doorbell exists
        bell_dir = agent_env["asdaaas_dir"] / "doorbells"
        compact_bells = list(bell_dir.glob("compact_*.json"))
        assert len(compact_bells) == 1, \
            f"Fix verified: expected 1 doorbell, got {len(compact_bells)}"

        # And it has correct numbers
        bell = json.load(open(compact_bells[0]))
        m = re.findall(r"from (\d+) to (\d+)", bell["text"])
        assert int(m[0][0]) == 150000
        assert int(m[0][1]) == 43000


# ============================================================================
# issue_0029: compaction reports wrong token counts
# ============================================================================

class TestCompactionTokenClobber:
    """Reproduce issue_0029: compaction reports wrong before/after token counts.

    Real-world scenario (Trip 2026-06-23):
      - Binary in retry loop (no_visible_content), writing _meta frames
      - Auto-compaction fires mid-retry
      - auto_compact_completed event written to updates.jsonl
      - Retry continues, writes more _meta frames with pre-compaction count
      - asdaaas reads all frames in collect_response via _process_update_frames
      - _meta processing (non-elif, runs on every frame) clobbers _total_tokens
      - Compaction report says "reduced from 164641 to 164643" (both pre-compaction)

    Two bugs identified:
      1. _process_update_frames: _meta check is `if` not `elif`, so it runs
         on the same frame as auto_compact_completed, clobbering _total_tokens
      2. If auto_compact_completed lacks tokens_after, pop_compaction_event
         returns 0, and `0 or total_tokens` falls back to clobbered value
    """

    def _make_backend_with_source(self, session_dir):
        """Create a GrokBackend wired to a real FileEventSource."""
        backend = GrokBackend.__new__(GrokBackend)
        backend._total_tokens = 0
        backend._compaction_event = None
        backend._compaction_tokens_before = 0
        backend._compaction_tokens_after = 0
        backend._last_activity_ts = 0.0
        backend._model_id = "test"
        backend._permission_pending = False
        backend._file_source = FileEventSource(session_dir)
        backend._file_source._updates_path = session_dir / "updates.jsonl"
        backend._file_source._updates_fp = open(session_dir / "updates.jsonl", "r")
        (session_dir / "events.jsonl").touch()
        backend._file_source._events_path = session_dir / "events.jsonl"
        backend._file_source._events_fp = open(session_dir / "events.jsonl", "r")
        return backend

    def _write_frame(self, path, frame):
        with open(path, "a") as f:
            f.write(json.dumps(frame) + "\n")

    def _meta_frame(self, total_tokens):
        return {
            "method": "_x.ai/session/update",
            "params": {
                "update": {"sessionUpdate": "agent_message_chunk",
                           "content": {"text": "x"}},
                "_meta": {"totalTokens": total_tokens}
            }
        }

    def _compaction_event_frame(self, tokens_after, tokens_before=None):
        update = {"sessionUpdate": "auto_compact_completed",
                  "tokens_after": tokens_after}
        if tokens_before is not None:
            update["tokens_before"] = tokens_before
        return {
            "method": "_x.ai/session/update",
            "params": {"update": update}
        }

    def test_meta_after_compaction_clobbers_total_tokens_in_refresh(self, tmp_path):
        """Bug 1: _meta frame AFTER auto_compact_completed in refresh_tokens
        clobbers _total_tokens back to pre-compaction value.

        refresh_tokens processes _meta BEFORE compaction check (line order),
        so if compaction event comes first and a _meta frame follows, the
        _meta frame wins for _total_tokens.
        """
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "updates.jsonl").touch()
        backend = self._make_backend_with_source(session_dir)
        updates_path = session_dir / "updates.jsonl"

        PRE_COMPACT = 164641
        POST_COMPACT = 18746
        STALE_META = 164643

        # Simulate: compaction event followed by stale _meta frame
        self._write_frame(updates_path, self._compaction_event_frame(POST_COMPACT, PRE_COMPACT))
        self._write_frame(updates_path, self._meta_frame(STALE_META))

        total = backend.refresh_tokens()

        # BUG: refresh_tokens processes frames sequentially. The compaction
        # event sets _total_tokens = 18746, then the _meta frame sets it
        # back to 164643.
        #
        # EXPECTED (after fix): total should be POST_COMPACT (18746)
        # ACTUAL (bug): total is STALE_META (164643)
        assert total == STALE_META, \
            f"Bug reproduced: refresh_tokens returned {total}, expected {STALE_META} (clobbered by _meta)"

        # But _compaction_tokens_after should still be correct
        found, event_tokens, event_before = backend.pop_compaction_event()
        assert found is True
        assert event_tokens == POST_COMPACT, \
            f"_compaction_tokens_after should be {POST_COMPACT}, got {event_tokens}"

    def test_meta_on_same_frame_clobbers_in_process_update(self, tmp_path):
        """Bug 2: In _process_update_frames, _meta is `if` not `elif`.

        If the auto_compact_completed frame itself carries _meta.totalTokens
        (which it does when embedded in a response stream), the non-elif _meta
        check runs on the same frame and clobbers _total_tokens.
        """
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "updates.jsonl").touch()
        backend = self._make_backend_with_source(session_dir)
        updates_path = session_dir / "updates.jsonl"

        PRE_COMPACT = 164641
        POST_COMPACT = 18746

        # Single frame: auto_compact_completed WITH _meta.totalTokens
        # This happens when the binary embeds the event in a response stream
        combined_frame = {
            "method": "_x.ai/session/update",
            "params": {
                "update": {
                    "sessionUpdate": "auto_compact_completed",
                    "tokens_after": POST_COMPACT,
                    "tokens_before": PRE_COMPACT
                },
                "_meta": {"totalTokens": PRE_COMPACT}
            }
        }
        self._write_frame(updates_path, combined_frame)

        # Process via _process_update_frames (same path as collect_response)
        frames = [combined_frame]
        backend._process_update_frames(
            frames, [], [], set(), None, None, None
        )

        # BUG: _process_update_frames handles auto_compact_completed (elif),
        # sets _total_tokens = POST_COMPACT. Then the non-elif _meta check
        # runs on the SAME frame and sets _total_tokens = PRE_COMPACT.
        #
        # EXPECTED (after fix): _total_tokens should be POST_COMPACT
        # ACTUAL (bug): _total_tokens is PRE_COMPACT
        assert backend._total_tokens == PRE_COMPACT, \
            f"Bug reproduced: _total_tokens={backend._total_tokens}, expected {PRE_COMPACT} (clobbered)"

        # _compaction_tokens_after should still be correct
        assert backend._compaction_tokens_after == POST_COMPACT

    def test_compaction_without_tokens_after_falls_back_to_clobbered(self, tmp_path):
        """Bug 3: If auto_compact_completed lacks tokens_after,
        pop_compaction_event returns 0, and asdaaas falls back to
        total_tokens which was clobbered by _meta.

        This reproduces the exact "164641 to 164643" report.
        """
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "updates.jsonl").touch()
        backend = self._make_backend_with_source(session_dir)
        updates_path = session_dir / "updates.jsonl"

        PRE_COMPACT = 164641
        STALE_META = 164643

        # Pre-compaction state
        self._write_frame(updates_path, self._meta_frame(PRE_COMPACT))
        backend.refresh_tokens()
        _prev_tokens = backend._total_tokens
        assert _prev_tokens == PRE_COMPACT

        # Compaction event WITHOUT tokens_after (some binary versions omit it)
        event_no_tokens = {
            "method": "_x.ai/session/update",
            "params": {
                "update": {
                    "sessionUpdate": "auto_compact_completed"
                    # no tokens_after field
                }
            }
        }
        self._write_frame(updates_path, event_no_tokens)
        # Stale _meta from retry
        self._write_frame(updates_path, self._meta_frame(STALE_META))

        backend.refresh_tokens()

        # pop_compaction_event returns event but with 0 tokens
        found, event_tokens, event_before = backend.pop_compaction_event()
        assert found is True
        assert event_tokens == 0, f"Expected 0 (no tokens_after in event), got {event_tokens}"

        # Simulate asdaaas line 2177-2178
        tokens_before = event_before or _prev_tokens
        tokens_after = event_tokens or backend._total_tokens

        # BUG: tokens_after falls back to _total_tokens which is STALE_META
        assert tokens_before == PRE_COMPACT
        assert tokens_after == STALE_META, \
            f"Bug reproduced: tokens_after={tokens_after}, expected {STALE_META} (fallback to clobbered total)"

        # This is exactly what the agent saw: "reduced from 164641 to 164643"
        assert abs(tokens_before - tokens_after) < 10, \
            "Both values are pre-compaction — compaction report is meaningless"