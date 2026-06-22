# Mock Binary Spec: Simulated grok Backend for E2E Testing

**Author:** Trip  
**Date:** 2026-06-22  
**Status:** Draft  
**Owner:** Trip (testing infrastructure)  
**Consult:** Sr (asdaaas internals)

---

## Problem

We can't test asdaaas's handling of binary error states because the real grok binary is a black box. We can't reliably make it produce `no_visible_content` retries, doom loops, or specific timing patterns on demand. Issue_0023 showed that asdaaas queues continues during retry state and floods them on resolve — but we have no way to write a regression test for the fix.

## Solution

A **MockBinary** — a Python class implementing the `AgentBackend` interface — that replaces `GrokBackend` in E2E tests. It speaks the same protocol asdaaas expects but is fully scriptable: the test tells it exactly what events to produce and when.

---

## Architecture

```
Test Script
    │
    ▼
MockBinary (implements AgentBackend)
    │  ▲
    │  │  send_prompt / collect_response / drain_stale / etc.
    ▼  │
asdaaas main loop (unchanged)
    │
    ▼
adapters, doorbells, command queue (real)
```

The test controls MockBinary via a **scenario script** — a sequence of actions the mock executes in response to `send_prompt` / `collect_response` calls. Everything else (adapters, doorbells, command queue, gaze, awareness) runs for real.

---

## AgentBackend Interface to Implement

From `agent_backend.py`:

| Method | MockBinary Behavior |
|---|---|
| `start(agent_cwd, model, session_id)` | Create session dir with empty updates.jsonl + events.jsonl. Return session_id. No subprocess. |
| `send_prompt(text)` | Record the prompt. Return a handle (incrementing int). |
| `collect_response(handle, ...)` | Execute the next step in the scenario script. Write events to updates.jsonl. Return `ResponseResult`. |
| `drain_stale()` | Return (0, "") unless scenario says otherwise. |
| `request_compaction()` | Simulate compaction if scenario says to. Write `auto_compact_completed` to updates.jsonl. |
| `shutdown()` | No-op (no subprocess to kill). |
| `proc` | Return None (no subprocess). |
| `session_id` | Return the test session ID. |
| `model_id` | Return "mock-model". |
| `refresh_tokens()` | Return scenario's current token count. |
| `pop_compaction_event()` | Return compaction event if scenario produced one. |
| `total_tokens` | Return scenario's current token count. |
| `context_window` | Return 200000. |

---

## Scenario Script Format

A scenario is a list of **steps**. Each step executes when `collect_response` is called (one step per agent turn).

```python
scenario = [
    # Step 1: Normal response
    NormalResponse(speech="Hello, I'm ready.", tokens=5000),

    # Step 2: End turn with tool call only (triggers no_visible_content retry)
    ToolCallOnly(
        tool_name="run_terminal_command",
        tool_input={"command": "echo delay"},
        retry_count=14,        # how many retry_state events before resolving
        retry_interval=0.5,    # seconds between retries (compressed from real ~30s)
        resolve_speech="Standing by.",  # what the turn eventually produces
    ),

    # Step 3: Doom loop (consecutive non-zero exits)
    DoomLoop(exit_count=5),

    # Step 4: Compaction
    Compaction(tokens_before=150000, tokens_after=30000),

    # Step 5: Empty response (agent says nothing)
    EmptyResponse(),

    # Step 6: Normal with delay command in speech
    NormalResponse(speech="Setting delay.", tokens=8000),
]
```

### Step Types

| Type | What it simulates | updates.jsonl events produced |
|---|---|---|
| `NormalResponse` | Clean turn with speech | `agent_message_chunk`, turn_ended |
| `ToolCallOnly` | Turn ends with tool call, no text | `tool_call`, `tool_call_update`, `retry_state` × N, then resolve with speech |
| `DoomLoop` | Consecutive non-zero exits | `doom_loop_detected` |
| `Compaction` | Auto-compaction event | `auto_compact_started`, `auto_compact_completed` |
| `EmptyResponse` | Turn with no speech or tools | turn_ended with empty speech |
| `SlowResponse` | Response that takes wall-clock time | `agent_message_chunk` with configurable delays between chunks |

### Timing Control

Real retry loops take minutes. Tests compress time:
- `retry_interval` controls delay between retry_state events (default 0.1s for fast tests)
- `SlowResponse` has `chunk_delay` for testing keepalive timeouts
- All timing is real wall-clock (asyncio.sleep), just shorter

---

## updates.jsonl Event Writing

MockBinary writes events to the real updates.jsonl in the session directory, in the same format GrokBackend produces. This means:
- asdaaas's `_process_update_frames()` works unchanged
- The audit tool (`audit_session.py`) can analyze mock sessions
- Test assertions can grep updates.jsonl just like real sessions

Event format matches the ACP session update spec:
```json
{"timestamp": 1782149292, "method": "session/update", "params": {"sessionId": "...", "update": {"sessionUpdate": "retry_state", ...}}}
```

---

## Integration with Test Harness

### Backend Selection

asdaaas already supports backend selection via `ASDAAAS_GROK_BINARY` env var and `config.agent_backend()`. Add a new backend type:

```python
# In asdaaas.py, extend backend creation
if backend_type == "mock":
    from mock_binary import MockBinary
    backend = MockBinary(scenario=scenario)
```

Or: the test instantiates MockBinary directly and injects it into the main loop, bypassing `start()`.

### Test Structure

```python
async def test_no_continues_during_retry():
    """issue_0023 regression: asdaaas must not generate continues during retry state."""
    scenario = [
        NormalResponse(speech="Ready.", tokens=5000),
        ToolCallOnly(retry_count=10, retry_interval=0.2, resolve_speech="Done."),
        NormalResponse(speech="Standing by.", tokens=6000),
    ]
    mock = MockBinary(scenario=scenario)

    # Run asdaaas main loop with mock backend
    # Inject a TUI message during step 2 (retry state)
    # Assert: no continue doorbells generated during retry window
    # Assert: injected message coalesced with queued tag
```

### What Runs Real vs Mock

| Component | Real or Mock |
|---|---|
| grok binary / LLM | **Mock** (MockBinary) |
| asdaaas main loop | Real |
| Adapter polling | Real |
| Command queue | Real |
| Doorbells | Real |
| Gaze/awareness | Real |
| updates.jsonl | Real file, mock-written events |
| health.json | Written by mock |

---

## Test Scenarios Enabled

| Scenario | What it tests | Issue |
|---|---|---|
| **Retry flood** | No continues during retry_state, messages coalesced on resolve | issue_0023 |
| **Doom loop** | asdaaas detects doom_loop_detected, stops sending prompts | general |
| **Compaction** | Token counts correct, orientation fires, context tag updates | issue_0022 |
| **Empty speech** | asdaaas handles turns with no output gracefully | general |
| **Slow response** | Keepalive timeout handling, cancel_event behavior | general |
| **Rapid turns** | Back-to-back fast responses, adapter polling keeps up | general |
| **Session resume** | Mock loads existing session, token state correct | general |

---

## File Location

```
~/projects/agent-abide/
  core/mock_binary.py          # MockBinary class
  tests/test_mock_scenarios.py # E2E tests using MockBinary
  tests/scenarios/             # Reusable scenario definitions (optional)
```

---

## Open Questions for Sr

1. **Backend injection:** What's the cleanest way to swap GrokBackend for MockBinary in the main loop? Env var + config, or direct injection via test helper?
2. **Main loop entry point:** Is there a way to run the main loop as an async function that returns (for test assertions), or do we need to run it in a task and inspect state externally?
3. **Retry state detection:** Does asdaaas currently see `retry_state` events from updates.jsonl, or does it only know about retries indirectly (via collect_response timing)?
4. **Continue generation:** Where exactly in the main loop are continues generated? Need to confirm the code path the fix will target.

---

## Next Steps

1. Sr reviews spec, answers open questions
2. Trip implements MockBinary (AgentBackend + scenario steps)
3. Trip writes first test: issue_0023 retry flood regression
4. Expand scenario library as new bugs surface
