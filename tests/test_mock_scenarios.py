"""
test_mock_scenarios.py -- E2E tests using MockBinary with asdaaas main loop.

These tests exercise real asdaaas behavior with scripted backend responses.
No LLM calls, no subprocess — fast and deterministic.

Run: cd ~/projects/agent-abide && python3 -m pytest tests/test_mock_scenarios.py -v
"""

import asyncio
import json
import os
import sys
import time
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

import pytest
from mock_binary import MockBinary, NormalResponse, ToolCallOnly, EmptyResponse, SlowResponse, CommandWriter, Compaction


AGENT_NAME = "MockTestAgent"
AGENTS_DIR = Path.home() / "agents"
AGENT_HOME = AGENTS_DIR / AGENT_NAME
AGENT_ABIDE = Path(__file__).resolve().parent.parent
AGENTS_JSON = AGENT_ABIDE / "agents.json"


def setup_mock_agent():
    """Create MockTestAgent directory structure for E2E tests."""
    for subdir in [
        "asdaaas/doorbells",
        "asdaaas/commands",
        "asdaaas/adapters/tui/inbox",
        "asdaaas/adapters/tui/outbox",
        "asdaaas/adapters/localmail/payloads",
        "asdaaas/adapters/localmail/inbox",
        "asdaaas/adapters/remind/inbox",
        "asdaaas/profile",
    ]:
        (AGENT_HOME / subdir).mkdir(parents=True, exist_ok=True)

    # Clean stale files
    for d in ["asdaaas/commands", "asdaaas/doorbells",
              "asdaaas/adapters/tui/inbox", "asdaaas/adapters/tui/outbox"]:
        target = AGENT_HOME / d
        for f in target.glob("*.json"):
            f.unlink()
    # Clear conversation log to prevent stale state from prior runs
    conv = AGENT_HOME / "asdaaas" / "conversation.jsonl"
    if conv.exists():
        conv.write_text("")
    # Also clean legacy commands.json
    legacy = AGENT_HOME / "asdaaas" / "commands.json"
    if legacy.exists():
        legacy.unlink()

    # Write awareness: direct_attach tui only, default_doorbell on
    awareness = {
        "direct_attach": ["tui"],
        "control_watch": {},
        "notify_watch": [],
        "accept_from": ["*"],
        "default_doorbell": True,
        "doorbell_ttl": {"default": 3},
    }
    with open(AGENT_HOME / "asdaaas" / "awareness.json", "w") as f:
        json.dump(awareness, f)

    # Write gaze: tui
    gaze = {"speech": {"target": "tui", "params": {}}, "thoughts": None}
    with open(AGENT_HOME / "asdaaas" / "gaze.json", "w") as f:
        json.dump(gaze, f)

    # Write AGENTS.md
    with open(AGENT_HOME / "AGENTS.md", "w") as f:
        f.write("# MockTestAgent\nRespond normally.\n")


def ensure_agent_in_config():
    """Add MockTestAgent to agents.json if missing."""
    with open(AGENTS_JSON) as f:
        config = json.load(f)

    agents = config.get("agents", {})
    if AGENT_NAME not in agents:
        agents[AGENT_NAME] = {
            "home": str(AGENT_HOME),
            "backend": "grok",
            "yolo": True,
        }
        config["agents"] = agents
        with open(AGENTS_JSON, "w") as f:
            json.dump(config, f, indent=2)


def inject_tui_message(text: str, sender: str = "eric"):
    """Write a message to MockTestAgent's TUI inbox."""
    inbox = AGENT_HOME / "asdaaas" / "adapters" / "tui" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    msg = {
        "from": sender,
        "adapter": "tui",
        "text": text,
        "id": f"msg_{int(time.time() * 1000)}",
    }
    path = inbox / f"msg_{int(time.time() * 1000)}.json"
    with open(path, "w") as f:
        json.dump(msg, f)


def inject_shutdown_command():
    """Tell asdaaas to shut down gracefully."""
    cmd_dir = AGENT_HOME / "asdaaas" / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    cmd = {"action": "shutdown"}
    path = cmd_dir / f"cmd_shutdown_{int(time.time() * 1000)}.json"
    with open(path, "w") as f:
        json.dump(cmd, f)


def read_outbox_messages() -> list[str]:
    """Read all TUI outbox messages."""
    outbox = AGENT_HOME / "asdaaas" / "adapters" / "tui" / "outbox"
    messages = []
    if outbox.exists():
        for f in sorted(outbox.glob("*.json")):
            try:
                with open(f) as fh:
                    msg = json.load(fh)
                messages.append(msg.get("text", ""))
            except (json.JSONDecodeError, OSError):
                pass
    return messages


def count_continue_doorbells() -> int:
    """Count continue doorbells in the doorbells directory."""
    bells_dir = AGENT_HOME / "asdaaas" / "doorbells"
    count = 0
    if bells_dir.exists():
        for f in bells_dir.glob("*.json"):
            try:
                with open(f) as fh:
                    bell = json.load(fh)
                if bell.get("type") == "continue" or "continue" in bell.get("text", "").lower():
                    count += 1
            except (json.JSONDecodeError, OSError):
                pass
    return count


@pytest.fixture(autouse=True)
def setup_teardown():
    """Setup and teardown for each test."""
    setup_mock_agent()
    ensure_agent_in_config()
    yield
    # Cleanup: remove gaze/awareness so they don't interfere with other tests
    for f in ["gaze.json", "awareness.json"]:
        p = AGENT_HOME / "asdaaas" / f
        if p.exists():
            p.unlink()


@pytest.mark.asyncio
async def test_normal_round_trip():
    """Basic: send message, get response via MockBinary."""
    scenario = [
        NormalResponse(speech="Hello from mock!", tokens=5000),
        EmptyResponse(tokens=5100),  # absorb continue
        EmptyResponse(tokens=5200),  # absorb continue
        EmptyResponse(tokens=5300),  # absorb continue
    ]
    mock = MockBinary(scenario)

    # Import main after sys.path setup
    from asdaaas import main, _shutdown_requested

    # Inject a message before starting
    inject_tui_message("Hi there")

    # Run main in a task, stop after first response
    async def run_and_stop():
        # Give main time to process one turn
        await asyncio.sleep(3)
        # Inject shutdown (until_event with no events = stops continue loop)
        inject_shutdown_command()
        await asyncio.sleep(1)

    import asdaaas
    asdaaas._shutdown_requested = False

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    stopper = asyncio.create_task(run_and_stop())

    try:
        await asyncio.wait_for(task, timeout=15)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        stopper.cancel()

    # Verify mock received the prompt (may not be last due to continues)
    assert mock.prompt_count >= 1
    assert any("Hi there" in p for p in mock.all_prompts), f"Expected 'Hi there' in prompts, got: {mock.all_prompts}"

    # Verify response made it to outbox
    outbox = read_outbox_messages()
    assert any("Hello from mock" in m for m in outbox), f"Expected mock response in outbox, got: {outbox}"


@pytest.mark.asyncio
async def test_no_continue_flood_after_empty_retry():
    """issue_0023 regression: empty retry response must not trigger continue cascade.

    Scenario: agent makes a tool-call-only turn. Binary retries internally.
    collect_response returns empty speech. A user message arrives during the
    retry window.

    Expected behavior (what the fix should achieve):
    1. After empty collect_response, asdaaas should check for pending messages
       BEFORE generating a continue doorbell
    2. The user message should be delivered directly — not after a continue
    3. No continue doorbells should fire between the empty response and the
       message delivery

    This test should FAIL until Sr's fix for issue_0023 lands.
    """
    scenario = [
        # Step 1: normal boot response
        NormalResponse(speech="Ready.", tokens=5000),
        # Step 2: tool-call-only with empty resolve (the retry scenario)
        # 3s simulates the binary's internal retry loop
        ToolCallOnly(retry_duration=3.0, resolve_speech="", tokens=6000),
        # Step 3: this should be the user's message, NOT a continue
        NormalResponse(speech="Got your message.", tokens=7000),
        # Steps 4-6: absorb any continues that fire (the bug)
        EmptyResponse(tokens=7100),
        EmptyResponse(tokens=7200),
        EmptyResponse(tokens=7300),
    ]
    mock = MockBinary(scenario)

    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    # Inject initial message to kick off conversation
    inject_tui_message("Start")

    async def inject_during_retry():
        # Wait for step 1 to complete and step 2 to start
        await asyncio.sleep(2)
        # Inject message while ToolCallOnly is blocking collect_response
        inject_tui_message("Important message during retry")
        # Wait for asdaaas to process everything
        await asyncio.sleep(8)
        # Stop
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    injector = asyncio.create_task(inject_during_retry())

    try:
        await asyncio.wait_for(task, timeout=25)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        injector.cancel()

    # === ASSERTIONS ===

    # 1. The user message must have been delivered
    assert any("Important message during retry" in p for p in mock.all_prompts), \
        f"User message never delivered. All prompts: {mock.all_prompts}"

    # 2. The FIRST prompt after the empty retry should contain the user message,
    #    NOT a continue doorbell. This is the core bug: asdaaas generates a
    #    continue before checking for pending messages.
    #    Prompt 0 = "Start", prompt 1 = continue (step 2 trigger),
    #    prompt 2 = should be user message, not another continue.
    prompts_after_start = [p for p in mock.all_prompts if "Start" not in p]
    user_msg_prompt_idx = None
    first_continue_after_retry_idx = None

    for i, p in enumerate(prompts_after_start):
        if "Important message during retry" in p:
            user_msg_prompt_idx = i
        if i == 0 and "continue" in p.lower():
            first_continue_after_retry_idx = i

    # The user message should arrive before or instead of a continue
    if user_msg_prompt_idx is not None and first_continue_after_retry_idx is not None:
        assert user_msg_prompt_idx <= first_continue_after_retry_idx, \
            "BUG (issue_0023): Continue doorbell fired before user message was delivered. " \
            f"User msg at index {user_msg_prompt_idx}, continue at {first_continue_after_retry_idx}"

    # 3. Count total continues — should be minimal (0-1), not a flood (3+)
    continue_prompts = [p for p in mock.all_prompts if "[continue" in p.lower()]
    assert len(continue_prompts) <= 1, \
        f"BUG (issue_0023): Continue flood detected. {len(continue_prompts)} continues fired. " \
        f"Expected 0-1. Continues: {continue_prompts[:3]}"


@pytest.mark.asyncio
async def test_delay_suppresses_continues_after_empty_retry():
    """issue_0023 variant: delay/ack after empty retry must suppress further continues.

    Scenario from Trip's 2026-06-22 15:26 session:
    1. Agent turn ends with tool call only (binary retries for ~6.5 min)
    2. Retry resolves, messages delivered, agent responds
    3. Agent sets 600s delay on the first continue
    4. BUG: two more continues fire at 8s intervals, ignoring the delay

    The agent's delay command must be respected. After acking a continue
    and setting a delay, no further continues should fire until the delay
    expires or an event interrupts.

    This test should FAIL until the delay-after-retry bug is fixed.
    """
    scenario = [
        # Step 1: normal response to initial message
        NormalResponse(speech="Ready.", tokens=5000),
        # Step 2: tool-call-only turn (empty retry, simulates binary retry window)
        ToolCallOnly(retry_duration=3.0, resolve_speech="", tokens=6000),
        # Step 3: response to continue (agent sets delay via command queue)
        NormalResponse(speech="Setting delay.", tokens=7000),
        # Steps 4-5: if continues leak through despite delay, these absorb them
        EmptyResponse(tokens=7100),
        EmptyResponse(tokens=7200),
        EmptyResponse(tokens=7300),
    ]
    mock = MockBinary(scenario)

    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    inject_tui_message("Start")

    continues_seen = []

    async def monitor_and_stop():
        # Wait for step 1 + step 2 (3s retry) + step 3 response
        await asyncio.sleep(6)

        # Now inject a delay command (simulating what the agent would do)
        # Agent responds to first continue and sets 600s delay
        cmd_dir = AGENT_HOME / "asdaaas" / "commands"
        cmd = {"action": "delay", "seconds": 600}
        path = cmd_dir / f"cmd_delay_{int(time.time() * 1000)}.json"
        with open(path, "w") as f:
            json.dump(cmd, f)

        # Wait to see if more continues fire despite the delay
        await asyncio.sleep(8)

        # Stop
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    monitor = asyncio.create_task(monitor_and_stop())

    try:
        await asyncio.wait_for(task, timeout=25)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        monitor.cancel()

    # Count continues after the first one
    continue_prompts = [p for p in mock.all_prompts if "[continue" in p.lower()]

    # At most 1 continue should fire (the initial one before delay is set).
    # The bug: 3 continues fire in rapid succession ignoring the delay.
    assert len(continue_prompts) <= 1, \
        f"BUG: {len(continue_prompts)} continues fired despite delay command. " \
        f"Expected at most 1. Delay should suppress further continues."


@pytest.mark.asyncio
async def test_stale_continues_purged_on_recovery():
    """issue_0023 variant 2: stale continues from timeout cycle must not survive recovery.

    Scenario (from Trip's 2026-06-22 15:26 session):
    1. Agent turn produces empty speech (simulates keepalive timeout during retry)
    2. This repeats — each empty response cycles the main loop, queuing a continue
    3. Recovery: user message arrives and gets delivered with speech
    4. BUG: stale continue doorbells from steps 1-2 fire AFTER recovery

    The stale continues are ghosts from the timeout cycle. After recovery
    delivers real user messages, those continues are meaningless and should
    be purged.
    """
    scenario = [
        # Step 1: normal boot
        NormalResponse(speech="Ready.", tokens=5000),
        # Steps 2-4: empty responses simulating keepalive timeouts during retry
        # Each one cycles the main loop and queues a continue doorbell
        EmptyResponse(tokens=5100),
        EmptyResponse(tokens=5200),
        EmptyResponse(tokens=5300),
        # Step 5: recovery — user message arrives, agent responds with speech
        NormalResponse(speech="Got the message after recovery.", tokens=6000),
        # Steps 6-13: absorb stale continues (need enough to avoid hanging
        # if the bug fires — 8 absorbers for 3 potential stale continues
        # plus margin for collection-window timing)
        EmptyResponse(tokens=6100),
        EmptyResponse(tokens=6200),
        EmptyResponse(tokens=6300),
        EmptyResponse(tokens=6400),
        EmptyResponse(tokens=6500),
        EmptyResponse(tokens=6600),
        EmptyResponse(tokens=6700),
        EmptyResponse(tokens=6800),
    ]
    mock = MockBinary(scenario)

    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    inject_tui_message("Start")

    async def inject_after_timeout_cycle():
        # Wait for boot + 3 empty timeout cycles
        await asyncio.sleep(5)
        # Inject user message (the recovery trigger)
        inject_tui_message("Recovery message")
        # Wait for processing
        await asyncio.sleep(12)
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    injector = asyncio.create_task(inject_after_timeout_cycle())

    try:
        await asyncio.wait_for(task, timeout=30)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        injector.cancel()

    # === ASSERTIONS ===

    # 1. Recovery message must have been delivered
    assert any("Recovery message" in p for p in mock.all_prompts), \
        f"Recovery message never delivered. All prompts: {[p[:80] for p in mock.all_prompts]}"

    # 2. Find where recovery happens in the prompt sequence
    recovery_idx = None
    for i, p in enumerate(mock.all_prompts):
        if "Recovery message" in p:
            recovery_idx = i
            break

    # 3. No continues should fire AFTER recovery. The continues from the
    #    timeout cycle (steps 2-4) are stale — they should be purged when
    #    the recovery message is delivered.
    prompts_after_recovery = mock.all_prompts[recovery_idx + 1:] if recovery_idx is not None else []
    continues_after_recovery = [p for p in prompts_after_recovery if "[continue" in p.lower()]

    assert len(continues_after_recovery) == 0, \
        f"BUG (issue_0023v2): {len(continues_after_recovery)} stale continues delivered AFTER recovery. " \
        f"These are ghosts from the timeout cycle and should have been purged. " \
        f"Continues: {[c[:80] for c in continues_after_recovery]}"


@pytest.mark.asyncio
async def test_until_event_delay_suppresses_queued_continues():
    """issue_0023 variant 3: until_event delay must suppress further continues.

    After responding to a user message with speech, asdaaas sets
    delay_until_event=True to wait for the agent's explicit delay command.
    This test verifies that when a second message arrives and the agent
    writes until_event, no further continues fire.

    Scenario:
    1. User sends msg1, agent responds
    2. User sends msg2, agent responds with until_event command
    3. Verify: no continues fire after until_event
    """
    scenario = [
        # Step 1: respond to first user message
        NormalResponse(speech="Got msg1.", tokens=5000),
        # Step 2: respond to second user message, set until_event
        CommandWriter(
            speech="Sleeping until event.",
            tokens=6000,
            commands=[{"action": "delay", "seconds": "until_event"}],
        ),
        # Steps 3-7: absorbers — if continues leak through despite
        # until_event, these catch them so the test doesn't hang
        EmptyResponse(tokens=6100),
        EmptyResponse(tokens=6200),
        EmptyResponse(tokens=6300),
        EmptyResponse(tokens=6400),
        EmptyResponse(tokens=6500),
    ]
    mock = MockBinary(scenario)

    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    inject_tui_message("msg1")

    async def send_second_and_stop():
        # Wait for first response, then send second message
        await asyncio.sleep(3)
        inject_tui_message("msg2")
        # Wait for second response + time for any stale continues
        await asyncio.sleep(15)
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    stopper = asyncio.create_task(send_second_and_stop())

    try:
        await asyncio.wait_for(task, timeout=25)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        stopper.cancel()

    # === ASSERTIONS ===

    # 1. Both messages were delivered to the agent
    assert len(mock.all_prompts) >= 2, \
        f"Expected at least 2 prompts (msg1 + msg2), got {len(mock.all_prompts)}: " \
        f"{[p[:60] for p in mock.all_prompts]}"

    # 2. The until_event speech was written to outbox
    outbox = read_outbox_messages()
    assert any("Sleeping until event" in m for m in outbox), \
        f"until_event speech not found in outbox. Outbox: {outbox}"

    # 3. No continues should fire AFTER until_event was set.
    # The agent's until_event command was processed after prompt 2 (msg2).
    # Any prompts beyond 2 are continues that leaked through.
    extra_prompts = mock.all_prompts[2:]
    continues_after_ue = [p for p in extra_prompts if "[continue" in p.lower()]

    assert len(continues_after_ue) == 0, \
        f"BUG (issue_0023v3): {len(continues_after_ue)} continues fired AFTER until_event. " \
        f"Agent set delay until_event but continues kept coming. " \
        f"Continues: {[c[:80] for c in continues_after_ue]}"


@pytest.mark.asyncio
async def test_midturn_messages_flagged_during_long_turn():
    """Messages sent while agent is working must get [sent during your previous turn] flag.

    Scenario (from Eric's 2026-06-22 report on Jr):
    1. User sends initial message, agent starts a long turn (~60s real time)
    2. User sends 2 more messages 20s and 40s into the turn
    3. Agent's turn completes, last_response_ts is set
    4. Next loop iteration polls messages — their _received_ts < last_response_ts
    5. Messages should be delivered with [sent during your previous turn] flag

    BUG: Both messages showed up as fresh new turns without the flag,
    triggering separate agent responses instead of being coalesced as
    midturn messages.

    Uses real wall-clock time (60s turn) because asdaaas midturn detection
    is based on real timestamps. Compressed tests can mask timing bugs.
    """
    scenario = [
        # Step 1: long agent turn — 60s real time
        SlowResponse(speech="Done with my long task.", delay=60.0, tokens=5000),
        # Step 2: response to the midturn messages (should have flags)
        NormalResponse(speech="Got your messages.", tokens=6000),
        # Steps 3-5: absorb continues
        EmptyResponse(tokens=6100),
        EmptyResponse(tokens=6200),
        EmptyResponse(tokens=6300),
    ]
    mock = MockBinary(scenario)

    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    # Initial message triggers the long turn
    inject_tui_message("Start working on the big task")

    async def inject_midturn_messages():
        # Wait 20s into the turn, send first message
        await asyncio.sleep(20)
        inject_tui_message("Hey, also check the config file")
        # Wait another 20s, send second message
        await asyncio.sleep(20)
        inject_tui_message("And update the README when you're done")
        # Wait for agent to finish long turn + process midturn messages
        await asyncio.sleep(30)
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    injector = asyncio.create_task(inject_midturn_messages())

    try:
        await asyncio.wait_for(task, timeout=90)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        injector.cancel()

    # === ASSERTIONS ===

    # 1. Both midturn messages must have been delivered
    all_prompts_text = "\n".join(mock.all_prompts)
    assert "also check the config file" in all_prompts_text, \
        f"First midturn message never delivered. Prompts: {[p[:100] for p in mock.all_prompts]}"
    assert "update the README" in all_prompts_text, \
        f"Second midturn message never delivered. Prompts: {[p[:100] for p in mock.all_prompts]}"

    # 2. Find the prompt(s) containing the midturn messages
    midturn_prompts = [p for p in mock.all_prompts
                       if "also check the config file" in p or "update the README" in p]

    # 3. Both messages must have the [sent during your previous turn] flag
    for prompt in midturn_prompts:
        for msg_text in ["also check the config file", "update the README"]:
            if msg_text in prompt:
                # Find the line containing this message
                for line in prompt.split("\n"):
                    if msg_text in line:
                        assert "sent during your previous turn" in line, \
                            f"BUG: Message '{msg_text}' delivered WITHOUT midturn flag. " \
                            f"Messages sent during a long agent turn must be flagged. " \
                            f"Line: {line}"

    # 4. Both messages should ideally be in the SAME prompt (coalesced),
    #    not delivered as separate turns
    same_prompt = any(
        "also check the config file" in p and "update the README" in p
        for p in mock.all_prompts
    )
    if not same_prompt:
        # Not a hard failure — the flag is what matters — but worth noting
        print("WARNING: midturn messages delivered in separate prompts instead of coalesced")


@pytest.mark.asyncio
async def test_midturn_messages_flagged_after_wall_clock_timeout():
    """Messages during a >10min turn where collect_response hits wall clock must be flagged.

    Scenario (from Eric's 2026-06-22 report on Jr):
    1. User sends initial message, agent starts a long turn
    2. Turn takes >600s, collect_response hits max_wall_clock, returns empty
    3. last_response_ts is set to NOW (the wall clock timeout moment)
    4. User had sent messages during the turn — they're in the inbox
    5. BUG: messages may not get [sent during your previous turn] flag because
       of continue doorbells or loop cycling between the timeout and the poll

    MockBinary simulates this as:
    - NormalResponse (initial speech, starts the "long turn")
    - EmptyResponse (simulates wall clock timeout — collect_response returns empty)
    - NormalResponse (handles whatever comes next — messages or continues)

    MockBinary writes tool_call activity to updates.jsonl during the test to
    prove the agent was working — updates.jsonl is the canonical activity signal.

    Uses real wall-clock time. Total test: ~75s.
    """
    scenario = [
        # Step 1: initial response, agent "starts working"
        NormalResponse(speech="Starting the big refactor.", tokens=5000),
        # Steps 2-4: empty returns simulating collect_response hitting
        # wall clock timeout mid-turn. Each one resets last_response_ts.
        # In reality these are 600s apart; here the loop cycles fast.
        EmptyResponse(tokens=5100),
        EmptyResponse(tokens=5200),
        EmptyResponse(tokens=5300),
        # Step 5: handle messages/continues after the "long turn"
        NormalResponse(speech="Got your messages after recovery.", tokens=6000),
        # Steps 6-8: absorb continues
        EmptyResponse(tokens=6100),
        EmptyResponse(tokens=6200),
        EmptyResponse(tokens=6300),
    ]
    mock = MockBinary(scenario)

    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    inject_tui_message("Start the big refactor")

    async def inject_midturn_and_activity():
        # Wait for initial response + a couple empty cycles
        await asyncio.sleep(3)

        # Simulate tool call activity in updates.jsonl during the "turn."
        # This is what a real agent does — writes tool results while working.
        # The activity proves the agent is still working even though
        # collect_response has returned empty.
        for i in range(4):
            mock._write_update_with_meta({
                "sessionUpdate": "tool_call_update",
                "toolCallId": f"tool_{i}",
                "title": f"Running command {i}",
                "status": "completed",
            }, 5100 + i * 10)
            await asyncio.sleep(1)

        # User sends messages while agent is "still working"
        # (updates.jsonl has recent activity)
        inject_tui_message("Hey when you're done check the tests too")
        await asyncio.sleep(2)
        inject_tui_message("Also the CI is failing on lint")

        # Wait for processing
        await asyncio.sleep(10)
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    injector = asyncio.create_task(inject_midturn_and_activity())

    try:
        await asyncio.wait_for(task, timeout=30)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        injector.cancel()

    # === ASSERTIONS ===

    all_prompts_text = "\n".join(mock.all_prompts)

    # 1. Both messages must have been delivered
    assert "check the tests too" in all_prompts_text, \
        f"First midturn message never delivered. Prompts: {[p[:100] for p in mock.all_prompts]}"
    assert "CI is failing on lint" in all_prompts_text, \
        f"Second midturn message never delivered. Prompts: {[p[:100] for p in mock.all_prompts]}"

    # 2. Both messages must have the [sent during your previous turn] flag
    for msg_text in ["check the tests too", "CI is failing on lint"]:
        for prompt in mock.all_prompts:
            if msg_text in prompt:
                for line in prompt.split("\n"):
                    if msg_text in line:
                        assert "sent during your previous turn" in line, \
                            f"BUG: Message '{msg_text}' delivered WITHOUT midturn flag. " \
                            f"Agent was still working (updates.jsonl has tool activity). " \
                            f"Messages sent during a long turn must be flagged even if " \
                            f"collect_response returned early due to wall clock timeout. " \
                            f"Line: {line}"


# ============================================================================
# Command processing tests -- CommandWriter scenarios
# ============================================================================


@pytest.mark.asyncio
async def test_agent_initiated_compaction():
    """Agent writes {"action": "compact"} during its turn.

    asdaaas should:
    1. Detect the compact command (via post-response drain + requeue, then step 1)
    2. Send /compact to the backend
    3. Update compaction_state.json to 'complete'
    4. Send a post-compaction orientation turn
    """
    scenario = [
        # Step 1: initial response to user message -- agent writes compact command
        CommandWriter(
            speech="I need to compact. Writing the command now.",
            tokens=150000,
            commands=[{"action": "compact"}],
        ),
        # Step 2: asdaaas sends /compact -- this is the compaction result
        Compaction(tokens_before=150000, tokens_after=30000),
        # Step 3: post-compaction orientation response
        NormalResponse(speech="Boot protocol complete.", tokens=31000),
        # Step 4+: absorb continues
        EmptyResponse(tokens=31100),
        EmptyResponse(tokens=31200),
        EmptyResponse(tokens=31300),
    ]
    mock = MockBinary(scenario)

    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    inject_tui_message("Please compact yourself")

    async def stop_after_processing():
        await asyncio.sleep(10)
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    stopper = asyncio.create_task(stop_after_processing())

    try:
        await asyncio.wait_for(task, timeout=25)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        stopper.cancel()

    # 1. /compact was sent to the backend
    assert any("/compact" in p for p in mock.all_prompts), \
        f"asdaaas never sent /compact. Prompts: {[p[:80] for p in mock.all_prompts]}"

    # 2. compaction_state.json shows complete
    state_path = AGENT_HOME / "asdaaas" / "compaction_state.json"
    assert state_path.exists(), "compaction_state.json not created"
    state = json.loads(state_path.read_text())
    assert state["phase"] == "complete", f"Expected phase=complete, got {state['phase']}"
    assert state["tokens_after"] is not None and state["tokens_after"] < state["tokens_before"], \
        f"Token reduction not recorded: before={state.get('tokens_before')}, after={state.get('tokens_after')}"

    # 3. Post-compaction orientation was sent (contains "boot protocol" or "Compaction complete")
    assert any("Compaction complete" in p for p in mock.all_prompts), \
        f"No post-compaction orientation prompt. Prompts: {[p[:100] for p in mock.all_prompts]}"


@pytest.mark.asyncio
async def test_gaze_command_changes_gaze_file():
    """Agent writes {"action": "gaze", "adapter": "irc", "room": "#standup"} during its turn.

    asdaaas should update gaze.json to reflect the new target.
    """
    scenario = [
        # Agent responds and sets gaze to IRC #standup
        CommandWriter(
            speech="Switching to standup channel.",
            tokens=5000,
            commands=[{"action": "gaze", "adapter": "irc", "room": "#standup"}],
        ),
        EmptyResponse(tokens=5100),
        EmptyResponse(tokens=5200),
    ]
    mock = MockBinary(scenario)

    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    inject_tui_message("Switch to standup")

    async def stop_after_processing():
        await asyncio.sleep(5)
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    stopper = asyncio.create_task(stop_after_processing())

    try:
        await asyncio.wait_for(task, timeout=15)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        stopper.cancel()

    # Verify gaze.json was updated
    gaze_path = AGENT_HOME / "asdaaas" / "gaze.json"
    assert gaze_path.exists(), "gaze.json not found"
    gaze = json.loads(gaze_path.read_text())
    speech_target = gaze.get("speech", {})
    assert speech_target.get("target") == "irc", \
        f"Expected gaze target=irc, got {speech_target}"
    params = speech_target.get("params", {})
    assert params.get("room") == "#standup", \
        f"Expected gaze room=#standup, got {params}"


@pytest.mark.asyncio
async def test_gaze_pm_command():
    """Agent writes {"action": "gaze", "adapter": "irc", "pm": "eric"} during its turn.

    asdaaas should update gaze.json to PM mode.
    """
    scenario = [
        CommandWriter(
            speech="Switching to PM with Eric.",
            tokens=5000,
            commands=[{"action": "gaze", "adapter": "irc", "pm": "eric"}],
        ),
        EmptyResponse(tokens=5100),
        EmptyResponse(tokens=5200),
    ]
    mock = MockBinary(scenario)

    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    inject_tui_message("PM eric")

    async def stop_after():
        await asyncio.sleep(5)
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    stopper = asyncio.create_task(stop_after())

    try:
        await asyncio.wait_for(task, timeout=15)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        stopper.cancel()

    gaze_path = AGENT_HOME / "asdaaas" / "gaze.json"
    gaze = json.loads(gaze_path.read_text())
    speech_target = gaze.get("speech", {})
    assert speech_target.get("target") == "irc", \
        f"Expected target=irc, got {speech_target}"
    params = speech_target.get("params", {})
    assert params.get("pm") == "eric", \
        f"Expected pm=eric, got {params}"


@pytest.mark.asyncio
async def test_awareness_add_channel():
    """Agent writes {"action": "awareness", "add": "#general", "mode": "doorbell"}.

    asdaaas should update awareness.json to include the new channel.
    """
    scenario = [
        CommandWriter(
            speech="Adding #general to awareness.",
            tokens=5000,
            commands=[{"action": "awareness", "add": "#general", "mode": "doorbell"}],
        ),
        EmptyResponse(tokens=5100),
        EmptyResponse(tokens=5200),
    ]
    mock = MockBinary(scenario)

    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    inject_tui_message("Watch general")

    async def stop_after():
        await asyncio.sleep(5)
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    stopper = asyncio.create_task(stop_after())

    try:
        await asyncio.wait_for(task, timeout=15)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        stopper.cancel()

    awareness_path = AGENT_HOME / "asdaaas" / "awareness.json"
    awareness = json.loads(awareness_path.read_text())
    bg = awareness.get("background_channels", {})
    assert "#general" in bg, \
        f"#general not found in background_channels. Got: {bg}"
    assert bg["#general"] == "doorbell", \
        f"Expected mode=doorbell, got {bg['#general']}"


@pytest.mark.asyncio
async def test_piggybacked_ack_clears_doorbell():
    """Agent writes {"action": "delay", "seconds": 0, "ack": ["<bell_id>"]}.

    The piggybacked ack should clear the doorbell from disk.
    """
    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    # Pre-create a doorbell that the agent will ack
    bell_dir = AGENT_HOME / "asdaaas" / "doorbells"
    bell_dir.mkdir(parents=True, exist_ok=True)
    bell = {
        "adapter": "localmail",
        "text": "Test message to ack",
        "ts": time.time(),
    }
    bell_file = bell_dir / "bell_test_ack_me.json"
    with open(bell_file, "w") as f:
        json.dump(bell, f)

    # The bell's id after poll_doorbells will be "bell_test_ack_me" (stem of filename)
    scenario = [
        # Agent responds to the doorbell + acks it with piggybacked delay
        CommandWriter(
            speech="Got it, acking.",
            tokens=5000,
            commands=[{"action": "delay", "seconds": 0, "ack": ["bell_test_ack_me"]}],
        ),
        EmptyResponse(tokens=5100),
        EmptyResponse(tokens=5200),
    ]
    mock = MockBinary(scenario)

    async def stop_after():
        await asyncio.sleep(5)
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    stopper = asyncio.create_task(stop_after())

    try:
        await asyncio.wait_for(task, timeout=15)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        stopper.cancel()

    # The doorbell file should be gone (acked)
    remaining = list(bell_dir.glob("bell_test_ack_me*"))
    assert len(remaining) == 0, \
        f"Doorbell not cleared by piggybacked ack. Remaining: {[f.name for f in remaining]}"


@pytest.mark.asyncio
async def test_compact_command_survives_post_response_drain():
    """issue_0028 regression: compact command written during response must survive
    the post-response drain and be processed on the next iteration.

    Before fix (87af75c), the drain consumed compact commands and dropped them.
    """
    scenario = [
        # Agent writes compact + delay in same turn (common pattern)
        CommandWriter(
            speech="Compacting now.",
            tokens=150000,
            commands=[
                {"action": "compact"},
                {"action": "delay", "seconds": 0},
            ],
        ),
        # asdaaas sends /compact
        Compaction(tokens_before=150000, tokens_after=30000),
        # Post-compaction orientation
        NormalResponse(speech="Rebooted.", tokens=31000),
        EmptyResponse(tokens=31100),
        EmptyResponse(tokens=31200),
    ]
    mock = MockBinary(scenario)

    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    inject_tui_message("Compact please")

    async def stop_after():
        await asyncio.sleep(10)
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    stopper = asyncio.create_task(stop_after())

    try:
        await asyncio.wait_for(task, timeout=25)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        stopper.cancel()

    # The compact command must have been processed (not silently dropped)
    assert any("/compact" in p for p in mock.all_prompts), \
        f"BUG (issue_0028): compact command dropped by post-response drain. " \
        f"Prompts: {[p[:80] for p in mock.all_prompts]}"

    state_path = AGENT_HOME / "asdaaas" / "compaction_state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
        assert state["phase"] == "complete", \
            f"Compaction did not complete: phase={state['phase']}"


@pytest.mark.asyncio
async def test_compaction_includes_default_instructions():
    """Agent-initiated compaction sends /compact with DEFAULT_COMPACTION_INSTRUCTIONS.

    When no per-agent compaction_instructions.txt exists, asdaaas should
    append the default instructions to the /compact prompt.
    """
    from asdaaas import DEFAULT_COMPACTION_INSTRUCTIONS

    # Make sure no per-agent file exists
    instructions_file = AGENT_HOME / "asdaaas" / "compaction_instructions.txt"
    if instructions_file.exists():
        instructions_file.unlink()

    scenario = [
        CommandWriter(
            speech="Compacting.",
            tokens=150000,
            commands=[{"action": "compact"}],
        ),
        Compaction(tokens_before=150000, tokens_after=30000),
        NormalResponse(speech="Done.", tokens=31000),
        EmptyResponse(tokens=31100),
        EmptyResponse(tokens=31200),
    ]
    mock = MockBinary(scenario)

    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    inject_tui_message("Compact")

    async def stop_after():
        await asyncio.sleep(10)
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    stopper = asyncio.create_task(stop_after())

    try:
        await asyncio.wait_for(task, timeout=25)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        stopper.cancel()

    # Find the /compact prompt and verify it includes instructions
    compact_prompts = [p for p in mock.all_prompts if p.startswith("/compact")]
    assert len(compact_prompts) >= 1, \
        f"No /compact prompt found. Prompts: {[p[:80] for p in mock.all_prompts]}"
    assert DEFAULT_COMPACTION_INSTRUCTIONS in compact_prompts[0], \
        f"/compact prompt missing default instructions. Got: {compact_prompts[0][:200]}"


@pytest.mark.asyncio
async def test_compaction_uses_per_agent_instructions():
    """Per-agent compaction_instructions.txt overrides default in /compact prompt."""
    custom_instructions = "Preserve: Trip's corrections log, TUI paste fix details, test backlog."

    instructions_file = AGENT_HOME / "asdaaas" / "compaction_instructions.txt"
    instructions_file.write_text(custom_instructions)

    try:
        scenario = [
            CommandWriter(
                speech="Compacting with custom instructions.",
                tokens=150000,
                commands=[{"action": "compact"}],
            ),
            Compaction(tokens_before=150000, tokens_after=30000),
            NormalResponse(speech="Done.", tokens=31000),
            EmptyResponse(tokens=31100),
            EmptyResponse(tokens=31200),
        ]
        mock = MockBinary(scenario)

        from asdaaas import main, DEFAULT_COMPACTION_INSTRUCTIONS
        import asdaaas
        asdaaas._shutdown_requested = False

        inject_tui_message("Compact with custom")

        async def stop_after():
            await asyncio.sleep(10)
            inject_shutdown_command()

        task = asyncio.create_task(
            main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
        )
        stopper = asyncio.create_task(stop_after())

        try:
            await asyncio.wait_for(task, timeout=25)
        except (asyncio.TimeoutError, SystemExit):
            pass
        finally:
            asdaaas._shutdown_requested = True
            stopper.cancel()

        compact_prompts = [p for p in mock.all_prompts if p.startswith("/compact")]
        assert len(compact_prompts) >= 1
        assert custom_instructions in compact_prompts[0], \
            f"/compact should use per-agent instructions. Got: {compact_prompts[0][:200]}"
        assert DEFAULT_COMPACTION_INSTRUCTIONS not in compact_prompts[0], \
            "Default instructions should NOT appear when per-agent file exists"
    finally:
        if instructions_file.exists():
            instructions_file.unlink()


@pytest.mark.asyncio
async def test_compaction_per_request_override():
    """{"action": "compact", "instructions": "..."} overrides both default and per-agent."""
    per_request = "Just keep the last 3 notebook entries."

    # Write a per-agent file too — per-request should override it
    instructions_file = AGENT_HOME / "asdaaas" / "compaction_instructions.txt"
    instructions_file.write_text("This should be overridden by per-request.")

    try:
        scenario = [
            CommandWriter(
                speech="Compacting with per-request override.",
                tokens=150000,
                commands=[{"action": "compact", "instructions": per_request}],
            ),
            Compaction(tokens_before=150000, tokens_after=30000),
            NormalResponse(speech="Done.", tokens=31000),
            EmptyResponse(tokens=31100),
            EmptyResponse(tokens=31200),
        ]
        mock = MockBinary(scenario)

        from asdaaas import main
        import asdaaas
        asdaaas._shutdown_requested = False

        inject_tui_message("Compact with override")

        async def stop_after():
            await asyncio.sleep(10)
            inject_shutdown_command()

        task = asyncio.create_task(
            main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
        )
        stopper = asyncio.create_task(stop_after())

        try:
            await asyncio.wait_for(task, timeout=25)
        except (asyncio.TimeoutError, SystemExit):
            pass
        finally:
            asdaaas._shutdown_requested = True
            stopper.cancel()

        compact_prompts = [p for p in mock.all_prompts if p.startswith("/compact")]
        assert len(compact_prompts) >= 1
        assert per_request in compact_prompts[0], \
            f"/compact should use per-request instructions. Got: {compact_prompts[0][:200]}"
    finally:
        if instructions_file.exists():
            instructions_file.unlink()


# ============================================================================
# BASIC CONTRACT: Agent controls its own turn pacing
# ============================================================================
# These tests verify the fundamental contract: when an agent writes a delay
# command, the system honors it. Without these, agents silently lose
# self-governance — they respond, the system waits, nobody tells the agent
# it needs to act, and the agent goes permanently silent.

@pytest.mark.asyncio
async def test_delay_zero_triggers_continue_after_doorbell():
    """Basic contract: agent writes delay 0 after a doorbell → gets a continue.

    This is the positive case for the most fundamental behavior:
    agent responds to a continue doorbell, writes delay 0, and receives
    another continue. Without this, agents cannot do multi-turn work.
    """
    scenario = [
        # Step 1: respond to user message (has_msgs guard fires,
        # but step 2 sends a second message to wake agent)
        NormalResponse(speech="Got it.", tokens=5000),
        # Step 2: respond to second message, write delay 0
        CommandWriter(
            speech="Working, need another turn.",
            tokens=6000,
            commands=[{"action": "delay", "seconds": 0}],
        ),
        # Step 3: this should be reached via continue doorbell
        NormalResponse(speech="Continue received.", tokens=7000),
        # Absorbers
        EmptyResponse(tokens=7100),
        EmptyResponse(tokens=7200),
        EmptyResponse(tokens=7300),
        EmptyResponse(tokens=7400),
        EmptyResponse(tokens=7500),
    ]
    mock = MockBinary(scenario)

    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    inject_tui_message("msg1")

    async def send_second_and_stop():
        await asyncio.sleep(3)
        inject_tui_message("msg2")
        await asyncio.sleep(15)
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    stopper = asyncio.create_task(send_second_and_stop())

    try:
        await asyncio.wait_for(task, timeout=25)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        stopper.cancel()

    # The agent should have gotten at least 3 prompts:
    # 1. msg1 (user message)
    # 2. msg2 (user message)
    # 3. continue (from delay 0)
    outbox = read_outbox_messages()
    assert any("Continue received" in m for m in outbox), \
        f"Agent wrote delay 0 but never got a continue. " \
        f"Outbox: {outbox}. Prompts: {[p[:60] for p in mock.all_prompts]}"


@pytest.mark.asyncio
async def test_agent_delay_honored_after_user_message():
    """Critical contract: agent writes delay 0 after user message → gets continue.

    This tests whether the agent's explicit delay command is honored even
    when the prompt contained a user message. The has_msgs guard (line 2838)
    currently overrides agent delay commands after user-message responses.
    If this test fails, agents lose self-governance after every user message.
    """
    scenario = [
        # Step 1: respond to user message, explicitly request continue
        CommandWriter(
            speech="Working on your request.",
            tokens=5000,
            commands=[{"action": "delay", "seconds": 0}],
        ),
        # Step 2: should arrive via continue — agent still working
        CommandWriter(
            speech="Still working.",
            tokens=6000,
            commands=[{"action": "delay", "seconds": 0}],
        ),
        # Step 3: done, set until_event
        CommandWriter(
            speech="Done. Standing by.",
            tokens=7000,
            commands=[{"action": "delay", "seconds": "until_event"}],
        ),
        # Absorbers
        EmptyResponse(tokens=7100),
        EmptyResponse(tokens=7200),
        EmptyResponse(tokens=7300),
        EmptyResponse(tokens=7400),
        EmptyResponse(tokens=7500),
    ]
    mock = MockBinary(scenario)

    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    inject_tui_message("Please do a multi-step task")

    async def stop_after():
        await asyncio.sleep(18)
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    stopper = asyncio.create_task(stop_after())

    try:
        await asyncio.wait_for(task, timeout=25)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        stopper.cancel()

    outbox = read_outbox_messages()

    # Agent should have completed all 3 steps
    assert any("Working on your request" in m for m in outbox), \
        f"Step 1 never delivered. Outbox: {outbox}"
    assert any("Still working" in m for m in outbox), \
        f"Step 2 never reached — delay 0 after user message was not honored. " \
        f"Agent lost self-governance. Outbox: {outbox}"
    assert any("Done. Standing by" in m for m in outbox), \
        f"Step 3 never reached. Outbox: {outbox}"


@pytest.mark.asyncio
async def test_delay_text_delivered_in_continue():
    """Contract: delay command with text field delivers that text in the continue.

    When an agent writes {"action": "delay", "seconds": 0, "text": "Continue: do X"},
    the continue doorbell should contain that text instead of the default message.
    """
    scenario = [
        # Step 1: respond to user message
        NormalResponse(speech="Got it.", tokens=5000),
        # Step 2: respond to second message, write delay 0 with text
        CommandWriter(
            speech="Starting work.",
            tokens=6000,
            commands=[{"action": "delay", "seconds": 0, "text": "Continue: finish items 7-10"}],
        ),
        # Step 3: should receive the directed text
        NormalResponse(speech="Finishing items.", tokens=7000),
        EmptyResponse(tokens=7100),
        EmptyResponse(tokens=7200),
        EmptyResponse(tokens=7300),
    ]
    mock = MockBinary(scenario)

    from asdaaas import main
    import asdaaas
    asdaaas._shutdown_requested = False

    inject_tui_message("msg1")

    async def send_second_and_stop():
        await asyncio.sleep(3)
        inject_tui_message("msg2")
        await asyncio.sleep(15)
        inject_shutdown_command()

    task = asyncio.create_task(
        main(AGENT_NAME, backend=mock, agent_cwd=str(AGENT_HOME))
    )
    stopper = asyncio.create_task(send_second_and_stop())

    try:
        await asyncio.wait_for(task, timeout=25)
    except (asyncio.TimeoutError, SystemExit):
        pass
    finally:
        asdaaas._shutdown_requested = True
        stopper.cancel()

    # The continue doorbell text should contain the agent's directed text
    outbox = read_outbox_messages()
    assert any("Finishing items" in m for m in outbox), \
        f"Directed continue never reached agent. Outbox: {outbox}. " \
        f"Prompts: {[p[:80] for p in mock.all_prompts]}"
    # Verify the directed text appeared in the prompt
    continue_prompts = [p for p in mock.all_prompts if "items 7-10" in p]
    assert len(continue_prompts) >= 1, \
        f"Delay text 'items 7-10' not found in any prompt. " \
        f"Prompts: {[p[:80] for p in mock.all_prompts]}"


# ============================================================================
# BASIC CONTRACT: Token tracking (end-to-end through real file pipeline)
# ============================================================================

def _write_updates_jsonl_frame(path, total_tokens, session_id="test-session"):
    """Write a realistic updates.jsonl frame with _meta.totalTokens."""
    import time as _time
    frame = {
        "timestamp": int(_time.time()),
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hello"},
            },
            "_meta": {
                "totalTokens": total_tokens,
                "eventId": f"{session_id}-{int(_time.time()*1000)}",
            },
        },
    }
    with open(path, "a") as f:
        f.write(json.dumps(frame) + "\n")


def test_token_ground_truth_reaches_system_state():
    """E2E contract: updates.jsonl _meta.totalTokens → GrokBackend.total_tokens → health.json.

    Ground truth for token count is _meta.totalTokens in updates.jsonl.
    This test verifies that value propagates through the real file-reading
    pipeline (FileEventSource → GrokBackend.refresh_tokens → total_tokens)
    and into health.json via write_health.

    If this fails, the TUI shows "ctx: 0%" and the agent loses context awareness.
    """
    import tempfile
    from grok_backend import GrokBackend, FileEventSource

    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir)
        updates_path = session_dir / "updates.jsonl"
        events_path = session_dir / "events.jsonl"

        # Create both files (FileEventSource needs both)
        updates_path.touch()
        events_path.touch()

        # Open FileEventSource — seeks to end of empty files
        fs = FileEventSource(session_dir)
        fs.open(timeout=1)

        # Write a frame with totalTokens=75000 AFTER opening (simulates live data)
        _write_updates_jsonl_frame(updates_path, total_tokens=75000)

        # Create backend and attach the file source
        backend = GrokBackend()
        backend._file_source = fs
        backend._total_tokens = 0  # start at 0, like real startup

        # refresh_tokens should read the new frame and update _total_tokens
        result = backend.refresh_tokens()

        assert result == 75000, \
            f"Ground truth: updates.jsonl has totalTokens=75000, " \
            f"but refresh_tokens returned {result}. " \
            f"Token data is not reaching the backend from the file pipeline."

        assert backend.total_tokens == 75000, \
            f"backend.total_tokens is {backend.total_tokens}, expected 75000"

        # Write another frame with higher count
        _write_updates_jsonl_frame(updates_path, total_tokens=80000)
        result2 = backend.refresh_tokens()

        assert result2 == 80000, \
            f"Second refresh: expected 80000, got {result2}. " \
            f"Token tracking stopped updating after first read."

        fs.close()


def test_context_left_banner_accurate():
    """E2E contract: given token state, context_left_tag produces correct banner.

    The banner "[Context left Xk till autocompaction | ...]" must reflect
    actual remaining context. This tests the full chain:
    updates.jsonl → refresh_tokens → total_tokens → context_left_tag → prompt.

    If this fails, the agent's prompt shows wrong context info or "0k".
    """
    import tempfile
    from grok_backend import FileEventSource
    from asdaaas import context_left_tag

    # Test 1: context_left_tag with known token values
    # 200k window, 85% threshold = 170k usable, 50k used = 120k left
    tag = context_left_tag(50000, 200000, turns_since_compaction=5)
    assert "Context left 120k" in tag, \
        f"Expected 'Context left 120k' for 50k/200k, got: {tag}"
    assert "compaction available" in tag, \
        f"Expected 'compaction available' after 5 turns, got: {tag}"

    # Test 2: context_left_tag with 0 tokens should return empty (no banner)
    tag_zero = context_left_tag(0, 200000)
    assert tag_zero == "", \
        f"context_left_tag should return empty string for 0 tokens, got: {tag_zero}"

    # Test 3: Full pipeline — file → backend → context_left_tag
    with tempfile.TemporaryDirectory() as tmpdir:
        session_dir = Path(tmpdir)
        updates_path = session_dir / "updates.jsonl"
        events_path = session_dir / "events.jsonl"
        updates_path.touch()
        events_path.touch()

        fs = FileEventSource(session_dir)
        fs.open(timeout=1)

        _write_updates_jsonl_frame(updates_path, total_tokens=100000)

        from grok_backend import GrokBackend
        backend = GrokBackend()
        backend._file_source = fs
        backend._total_tokens = 0

        tokens = backend.refresh_tokens()
        tag = context_left_tag(tokens, 200000, turns_since_compaction=3)

        # 200k * 0.85 = 170k usable, 100k used = 70k left
        assert "Context left 70k" in tag, \
            f"Full pipeline: expected 'Context left 70k' for 100k/200k, got: {tag}"
        assert "Context left 0k" not in tag, \
            f"Banner shows 0k — token tracking pipeline broken. Tag: {tag}"

        fs.close()