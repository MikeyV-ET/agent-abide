# T1: conftest.py Fixture API Spec

**Author:** Trip  
**Date:** 2026-07-04  
**For:** Sr (S4 phase method design)  
**Status:** Draft for review

---

## Goal

A single `asdaaas_env` pytest fixture that makes writing a true e2e test expressible in ~5 lines. All state is hermetic (tmp_path), no writes to real ~/agents, no mutations to repo files.

## Definition: True E2E Test

A test named `test_e2e_*` must:
- Drive `main()` (or its successor) with MockBinary as the backend
- Input only through the public file interface (adapter inboxes, doorbells, commands)
- Assert only on public outputs (outboxes, health file, doorbell/command state, conversation.jsonl)
- NOT import private functions from asdaaas

## Fixture: `asdaaas_env`

```python
@pytest.fixture
def asdaaas_env(tmp_path) -> AsdaaasTestEnv:
    """
    Builds full agent directory tree under tmp_path, writes test config,
    constructs AsdaaasEnv, yields a test handle.
    """
```

### The AsdaaasTestEnv handle

```python
class AsdaaasTestEnv:
    """Test harness wrapping a hermetic asdaaas instance."""

    # --- Paths ---
    env: AsdaaasEnv           # The injected composition root (from S1)
    agent_name: str           # "TestAgent"
    agent_home: Path          # tmp_path / "agents" / "TestAgent"
    asdaaas_dir: Path         # agent_home / "asdaaas"

    # --- Input: inject into the agent's world ---

    def inject_message(self, adapter: str, text: str, sender: str = "eric") -> Path:
        """Write a message to adapter inbox. Returns the file path."""

    def inject_doorbell(self, doorbell_id: str, adapter: str = "tui",
                        sender: str = "eric", text: str = "") -> Path:
        """Write a doorbell to the doorbells dir."""

    def inject_command(self, command: dict) -> Path:
        """Write a command to the commands dir."""

    def inject_localmail(self, from_agent: str, text: str) -> Path:
        """Write a localmail message to the inbox."""

    # --- Output: read from the agent's world ---

    def outbox(self, adapter: str = "tui") -> list[dict]:
        """Return parsed outbox messages for adapter, sorted by timestamp."""

    def health(self) -> dict:
        """Return parsed health.json."""

    def doorbells(self) -> list[dict]:
        """Return all active doorbells, sorted by timestamp."""

    def commands(self) -> list[dict]:
        """Return all pending commands, sorted by timestamp."""

    def conversation(self) -> list[dict]:
        """Return parsed conversation.jsonl entries."""

    def gaze(self) -> dict:
        """Return current gaze.json."""

    def awareness(self) -> dict:
        """Return current awareness.json."""

    # --- Execution: run the engine ---

    async def run_main(
        self,
        scenario: list,           # MockBinary step list
        until: Callable = None,   # Predicate: stop when true
        max_turns: int = 10,      # Safety cap
        timeout: float = 10.0,    # Wall-clock seconds (compressed via S2 Clock)
        context_window: int = 200000,
    ) -> RunResult:
        """
        Run asdaaas main() with MockBinary as backend.
        
        The scenario is a list of MockBinary step types
        (NormalResponse, ShellToolCall, Compaction, etc).
        
        Stops when:
          - MockBinary exhausts its scenario
          - until() predicate returns True
          - max_turns reached
          - timeout exceeded
        
        Returns RunResult with turn count, final state, and any errors.
        """

    async def run_turn(
        self,
        scenario: list,
    ) -> TurnResult:
        """
        Run a SINGLE turn phase cycle. This is the key seam from S4.
        
        Phase cycle:
          1. gather_pending() — collect doorbells, adapter messages, commands
          2. deliver_turn() — build prompt, send to backend, collect response
          3. post_turn() — process commands, drain, update health
        
        Returns TurnResult with what was gathered, delivered, and produced.
        """

    # --- Phase-level access (needs S4 seams) ---

    async def gather(self) -> GatherResult:
        """Run gather_pending() phase only. Returns what was collected."""

    async def deliver(self, gathered: GatherResult, scenario: list) -> DeliverResult:
        """Run deliver_turn() phase only. Returns response + side effects."""

    async def post_turn(self, deliver_result: DeliverResult) -> PostTurnResult:
        """Run post_turn() phase only. Returns commands processed, health state."""

    # --- Helpers ---

    def wait_for_outbox(self, adapter: str = "tui",
                        count: int = 1, timeout: float = 5.0) -> list[dict]:
        """Poll until at least count messages appear in outbox."""

    def clear_outbox(self, adapter: str = "tui"):
        """Remove all outbox files for adapter."""

    def clear_doorbells(self):
        """Remove all doorbell files."""
```

## What this needs from S4 (phase method seams)

The `run_turn()` and phase-level methods (`gather`, `deliver`, `post_turn`) need S4 to decompose main() into callable phase methods. The key contract:

### Phase 1: gather_pending(env) → GatherResult
- Polls doorbells, adapter inboxes, commands, localmail
- Returns structured result of what was found
- Does NOT modify agent state yet (or modifications are part of the result)

### Phase 2: deliver_turn(env, gathered, backend) → DeliverResult
- Builds the prompt from gathered input
- Calls backend.send_prompt() / collect_response()
- Spawns interjection watcher if enabled
- Returns the response and any interjections delivered

### Phase 3: post_turn(env, deliver_result) → PostTurnResult
- Processes commands from the response
- Updates health.json
- Drains post-turn state
- Queues continue doorbell if needed
- Returns what was processed

### Idle / Compaction as separate paths
- handle_idle(env) — the delay/sleep logic
- handle_compaction(env, backend) — compaction flow including conditional drain
- handle_orientation(env, backend) — post-compaction first turn

Each of these should take `env: AsdaaasEnv` as the first argument (from S1).

## What this needs from S2 (injectable clock)

`run_main()` needs compressed time so tests don't wait on real poll intervals. The Clock object on AsdaaasEnv should support:
- `clock.sleep(seconds)` — in tests, returns immediately or advances simulated time
- `clock.now()` — current time (real or simulated)
- `clock.poll_interval` — configurable, set to 0 in tests

## Example: what a true e2e test looks like with this fixture

```python
async def test_normal_round_trip(asdaaas_env):
    env = asdaaas_env
    env.inject_message("tui", "hello", sender="eric")
    result = await env.run_main(
        scenario=[NormalResponse("Hello back!")],
        until=lambda: len(env.outbox("tui")) >= 1,
    )
    assert env.outbox("tui")[0]["text"] == "Hello back!"
    assert result.turns == 1

async def test_interjection_during_compaction(asdaaas_env):
    env = asdaaas_env
    env.inject_message("tui", "do some work", sender="eric")
    result = await env.run_main(
        scenario=[
            NormalResponse("Working on it."),
            Compaction(),
            ShellToolCall("echo boot"),  # orientation turn
        ],
        until=lambda: result.turns >= 3,
    )
    # Inject message during orientation (simulating Eric's STOP)
    # ... this is where the interjection watcher picks it up
```

## Non-goals

- This spec does NOT cover TUI widget testing (separate concern)
- This spec does NOT define MockBinary changes (it's already well-designed)
- This spec does NOT prescribe S4 implementation — only the test surface needed

---

## Open questions for Sr

1. Should phase methods be standalone functions or methods on a TurnEngine class?
2. Should `gather_pending()` drain adapter inboxes (destructive) or peek (non-destructive with explicit consume)?
3. Is the phase boundary between deliver and post_turn clean enough to test independently, or is there shared state that bleeds?
