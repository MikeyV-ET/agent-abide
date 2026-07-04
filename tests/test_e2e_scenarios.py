"""T5: Behavioral e2e scenario tests.

Each test exercises a real behavioral scenario through the full phase cycle
using MockBinary and the AsdaaasTestEnv fixture. Input only through file
interface, assertions only on public outputs.
"""
import pytest


class TestEmptyResponseBackoff:
    """Consecutive empty doorbell responses trigger increasing delay backoff."""

    @pytest.mark.asyncio
    async def test_backoff_after_threshold(self, asdaaas_env):
        """After 3 consecutive empty doorbell responses, next_turn_delay increases."""
        from mock_binary import MockBinary, NormalResponse
        # 4 turns of empty speech to exceed EMPTY_DOORBELL_BACKOFF_AFTER (3)
        mock = MockBinary([
            NormalResponse(speech="", tokens=5000),
            NormalResponse(speech="", tokens=5000),
            NormalResponse(speech="", tokens=5000),
            NormalResponse(speech="", tokens=5000),
        ])
        engine = asdaaas_env.make_engine(backend=mock)

        for i in range(4):
            asdaaas_env.inject_doorbell(f"bell_bo_{i}", adapter="tui", sender="system", text=f"Empty {i}")
            _, dr, ptr = await asdaaas_env.run_turn(engine)
            assert ptr.speech_delivered is False

        # After 4 empties (> threshold of 3), backoff should be active
        assert engine.consecutive_empty_doorbell == 4
        assert engine.next_turn_delay > 0

    @pytest.mark.asyncio
    async def test_backoff_resets_on_speech(self, asdaaas_env):
        """Speech response resets consecutive_empty_doorbell counter."""
        from mock_binary import MockBinary, NormalResponse
        mock = MockBinary([
            NormalResponse(speech="", tokens=5000),
            NormalResponse(speech="", tokens=5000),
            NormalResponse(speech="", tokens=5000),
            NormalResponse(speech="Got it!", tokens=5000),
        ])
        engine = asdaaas_env.make_engine(backend=mock)

        # 3 empty turns
        for i in range(3):
            asdaaas_env.inject_doorbell(f"bell_br_{i}", adapter="tui", sender="system", text=f"Empty {i}")
            await asdaaas_env.run_turn(engine)
        assert engine.consecutive_empty_doorbell == 3

        # Turn with speech resets
        asdaaas_env.inject_doorbell("bell_br_3", adapter="tui", sender="eric", text="Say something")
        _, dr, ptr = await asdaaas_env.run_turn(engine)
        assert dr.speech == "Got it!"
        assert engine.consecutive_empty_doorbell == 0


class TestAckAcrossTurns:
    """Ack commands clear doorbells and affect subsequent turns."""

    @pytest.mark.asyncio
    async def test_ack_prevents_redelivery(self, asdaaas_env):
        """Acked doorbell doesn't appear in next gather."""
        from mock_binary import MockBinary, NormalResponse
        mock = MockBinary([
            NormalResponse(speech="Seen it.", tokens=5000),
            NormalResponse(speech="Nothing new.", tokens=5000),
        ])
        engine = asdaaas_env.make_engine(backend=mock)

        # Turn 1: doorbell delivered
        asdaaas_env.inject_doorbell("bell_once", adapter="tui", sender="eric", text="See this once")
        asdaaas_env.inject_command({"action": "ack", "handled": ["bell_once"]})
        g1, d1, p1 = await asdaaas_env.run_turn(engine)
        assert d1.speech == "Seen it."

        # Turn 2: doorbell should be gone
        g2 = await engine.gather_pending()
        assert not any(b.get("id") == "bell_once" for b in g2.doorbells)

    @pytest.mark.asyncio
    async def test_piggyback_ack_on_delay(self, asdaaas_env):
        """Delay command with piggyback ack clears doorbell."""
        from mock_binary import MockBinary, NormalResponse
        mock = MockBinary([NormalResponse(speech="OK.", tokens=5000)])
        engine = asdaaas_env.make_engine(backend=mock)

        asdaaas_env.inject_doorbell("bell_piggy", adapter="tui", sender="eric", text="Ack me via piggyback")
        asdaaas_env.inject_command({"action": "delay", "seconds": 300, "ack": ["bell_piggy"]})
        _, dr, ptr = await asdaaas_env.run_turn(engine)
        assert ptr.agent_wrote_delay is True
        remaining = asdaaas_env.doorbells()
        assert not any(d.get("id") == "bell_piggy" for d in remaining)


class TestMultipleDoorbelCoalesce:
    """Multiple doorbells in single turn coalesce into one prompt."""

    @pytest.mark.asyncio
    async def test_three_doorbells_one_prompt(self, asdaaas_env):
        """Three doorbells → single prompt containing all three."""
        from mock_binary import MockBinary, NormalResponse
        mock = MockBinary([NormalResponse(speech="All received.", tokens=5000)])
        engine = asdaaas_env.make_engine(backend=mock)

        asdaaas_env.inject_doorbell("bell_m1", adapter="tui", sender="eric", text="Message one")
        asdaaas_env.inject_doorbell("bell_m2", adapter="tui", sender="eric", text="Message two")
        asdaaas_env.inject_doorbell("bell_m3", adapter="tui", sender="eric", text="Message three")

        g, dr, ptr = await asdaaas_env.run_turn(engine)
        assert len(g.doorbells) == 3
        assert mock.prompt_count == 1
        assert "Message one" in mock.last_prompt
        assert "Message two" in mock.last_prompt
        assert "Message three" in mock.last_prompt


class TestGazeRouting:
    """Speech routes to the correct outbox based on gaze."""

    @pytest.mark.asyncio
    async def test_speech_routes_to_gaze_target(self, asdaaas_env):
        """Speech goes to the adapter specified in gaze."""
        from mock_binary import MockBinary, NormalResponse
        import json

        mock = MockBinary([NormalResponse(speech="Hello IRC!", tokens=5000)])
        engine = asdaaas_env.make_engine(backend=mock)

        # Set gaze to IRC #test channel
        gaze_path = asdaaas_env.asdaaas_dir / "gaze.json"
        gaze_path.parent.mkdir(parents=True, exist_ok=True)
        gaze_path.write_text(json.dumps({
            "speech": {"adapter": "irc", "room": "#test"},
            "adapter": "irc",
            "room": "#test"
        }))

        asdaaas_env.inject_doorbell("bell_gz", adapter="tui", sender="eric", text="Say something")
        _, dr, ptr = await asdaaas_env.run_turn(engine)
        assert dr.speech == "Hello IRC!"
        assert ptr.speech_delivered is True

        # Check that outbox was written for irc adapter
        irc_outbox = asdaaas_env.outbox("irc")
        assert len(irc_outbox) > 0


class TestCompactionDetection:
    """Compaction detection via token drop heuristic."""

    @pytest.mark.asyncio
    async def test_heuristic_compaction_detected(self, asdaaas_env):
        """Token count dropping >40% triggers compaction detection."""
        from mock_binary import MockBinary, NormalResponse
        mock = MockBinary([
            NormalResponse(speech="Before compaction.", tokens=100000),
        ])
        engine = asdaaas_env.make_engine(backend=mock)

        # Turn 1: establish high token count + set _prev_tokens
        asdaaas_env.inject_doorbell("bell_c1", adapter="tui", sender="eric", text="First turn")
        await asdaaas_env.run_turn(engine)
        assert engine.total_tokens == 100000
        # Call handle_compaction_detection to set _prev_tokens
        await engine.handle_compaction_detection()
        assert engine._prev_tokens == 100000

        # Simulate token drop (as if binary compacted)
        engine.total_tokens = 30000
        engine.backend._total_tokens = 30000

        # Heuristic should detect: 30000 < 100000 * 0.6
        detected = await engine.handle_compaction_detection()
        assert detected is True
        assert engine.turns_since_compaction == 0
