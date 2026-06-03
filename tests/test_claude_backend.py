#!/usr/bin/env python3
"""
test_claude_backend.py — Smoke test for ClaudeBackend.

Sends a simple prompt, collects the response, verifies the protocol works.
Requires claude CLI to be installed and authenticated.

Usage:
    python test_claude_backend.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))

from claude_backend import ClaudeBackend


async def test_basic_prompt():
    """Send a simple prompt and verify we get a response."""
    print("=== Test 1: Basic prompt ===")
    backend = ClaudeBackend()

    try:
        session_id = await backend.start(
            agent_cwd=os.path.expanduser("~/agents/Sr"),
        )
        print(f"  Session: {session_id}")
        print(f"  Model: {backend.model_id}")
        print(f"  PID: {backend.proc.pid}")

        handle = await backend.send_prompt("Reply with exactly one word: VERIFIED")

        speech_chunks = []
        def on_chunk(text):
            speech_chunks.append(text)

        result = await backend.collect_response(
            handle,
            on_speech_chunk=on_chunk,
            keepalive_timeout=15.0,
            max_wall_clock=30.0,
        )

        print(f"  Speech: {result.speech!r}")
        print(f"  Thoughts: {result.thoughts[:80]!r}" if result.thoughts else "  Thoughts: (none)")
        print(f"  Tokens: {result.total_tokens}")
        print(f"  Model: {result.model_id}")
        print(f"  Session (post): {backend.session_id}")
        print(f"  Stop: {result.stop_reason}")
        print(f"  Cost: ${result.cost_usd:.4f}")
        print(f"  Context window: {backend.context_window}")
        print(f"  Chunks received: {len(speech_chunks)}")

        assert result.speech, "Expected non-empty speech"
        assert result.total_tokens > 0, "Expected token count > 0"
        assert "unknown" not in backend.model_id, f"Model should be identified, got: {backend.model_id}"
        print("  PASSED")

    finally:
        await backend.shutdown()
        print(f"  Shutdown complete")


async def test_multi_turn():
    """Send two prompts in sequence to verify state persistence."""
    print("\n=== Test 2: Multi-turn conversation ===")
    backend = ClaudeBackend()

    try:
        await backend.start(agent_cwd=os.path.expanduser("~/agents/Sr"))

        # Turn 1
        handle = await backend.send_prompt("Remember this number: 42. Reply OK.")
        result1 = await backend.collect_response(handle, keepalive_timeout=15.0)
        tokens_after_t1 = backend.total_tokens
        print(f"  Turn 1: {result1.speech!r} ({tokens_after_t1} tokens)")

        # Turn 2
        handle = await backend.send_prompt("What number did I ask you to remember? Reply with just the number.")
        result2 = await backend.collect_response(handle, keepalive_timeout=15.0)
        tokens_after_t2 = backend.total_tokens
        print(f"  Turn 2: {result2.speech!r} ({tokens_after_t2} tokens)")

        assert tokens_after_t2 > tokens_after_t1, "Token count should increase across turns"
        assert "42" in result2.speech, f"Expected '42' in response, got: {result2.speech!r}"
        print("  PASSED")

    finally:
        await backend.shutdown()
        print(f"  Shutdown complete")


async def test_drain():
    """Verify drain_stale works (should return 0 on fresh pipe)."""
    print("\n=== Test 3: Drain stale frames ===")
    backend = ClaudeBackend()

    try:
        await backend.start(agent_cwd=os.path.expanduser("~/agents/Sr"))

        count, speech = await backend.drain_stale()
        print(f"  Drained: {count} frames, speech: {speech!r}")
        assert count == 0, "Expected 0 stale frames on fresh backend"
        print("  PASSED")

    finally:
        await backend.shutdown()
        print(f"  Shutdown complete")


async def test_compaction():
    """Verify request_compaction returns False (not supported)."""
    print("\n=== Test 4: Compaction (should be no-op) ===")
    backend = ClaudeBackend()

    try:
        await backend.start(agent_cwd=os.path.expanduser("~/agents/Sr"))

        result = await backend.request_compaction()
        print(f"  Compaction supported: {result}")
        assert result is False, "Claude backend should not support compaction"
        print("  PASSED")

    finally:
        await backend.shutdown()
        print(f"  Shutdown complete")


async def main():
    print("ClaudeBackend Smoke Tests")
    print("=" * 40)

    await test_basic_prompt()
    await test_multi_turn()
    await test_drain()
    await test_compaction()

    print("\n" + "=" * 40)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
