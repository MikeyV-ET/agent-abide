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
from mock_binary import MockBinary, NormalResponse, ToolCallOnly, EmptyResponse


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
    cmd = {"action": "delay", "seconds": "until_event"}
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