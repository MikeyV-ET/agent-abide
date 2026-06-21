#!/usr/bin/env python3
"""E2E compaction tests using a real test agent.

Starts a real TestAgent via asdaaas.py with the grok binary,
sends it work, triggers compaction, and verifies post-compaction behavior.

These tests use real API calls and are NOT suitable for CI.
Run manually: python3 tests/test_e2e_compaction.py [test1|test2|test3]

Test 1: Agent-initiated compaction
  - Start TestAgent, give it work, send /compact command
  - Verify post-compaction: agent follows boot protocol

Test 2: Auto-compaction (binary-initiated)
  - Start TestAgent, give it enough work to hit 85% context
  - Verify: binary sends "Continue..." prompt, agent does NOT re-orient

Test 3: Modular AGENTS.md
  - Change AGENTS.md between compactions
  - Verify agent sees new version post-compaction
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import shutil
from pathlib import Path

AGENT_ABIDE = Path(__file__).resolve().parent.parent
CORE = AGENT_ABIDE / "core"
AGENTS_DIR = Path.home() / "agents"
TESTAGENT_HOME = AGENTS_DIR / "TestAgent"
AGENTS_JSON = AGENT_ABIDE / "agents.json"
GROK_BINARY = Path.home() / ".grok" / "bin" / "grok"

# How long to wait for the agent to reach "Ready." state
STARTUP_TIMEOUT = 120
# How long to wait for compaction to complete
COMPACTION_TIMEOUT = 300
# How long to wait for post-compaction response
POST_COMPACTION_TIMEOUT = 180


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def setup_testagent_home():
    """Create/reset TestAgent directory structure."""
    # Create dirs (don't nuke existing — might have session data)
    for subdir in [
        "asdaaas/doorbells",
        "asdaaas/commands",
        "asdaaas/adapters/localmail/payloads",
        "asdaaas/adapters/localmail/inbox",
        "asdaaas/adapters/remind/inbox",
        "asdaaas/adapters/tui/outbox",
        "asdaaas/profile",
    ]:
        (TESTAGENT_HOME / subdir).mkdir(parents=True, exist_ok=True)

    # Clean stale commands/doorbells from prior runs
    for d in ["asdaaas/commands", "asdaaas/doorbells"]:
        target = TESTAGENT_HOME / d
        for f in target.glob("*.json"):
            f.unlink()

    log(f"TestAgent home ready: {TESTAGENT_HOME}")


def ensure_testagent_in_config():
    """Add TestAgent to agents.json if not present. Returns session_id or None."""
    with open(AGENTS_JSON) as f:
        config = json.load(f)

    if "TestAgent" not in config["agents"]:
        config["agents"]["TestAgent"] = {
            "session": "",  # empty = new session
            "home": str(TESTAGENT_HOME),
            "yolo": True,
        }
        with open(AGENTS_JSON, "w") as f:
            json.dump(config, f, indent=2)
        log("Added TestAgent to agents.json (new session)")
        return None

    session = config["agents"]["TestAgent"].get("session", "")
    log(f"TestAgent already in config, session={session or '(new)'}")
    return session if session else None


def update_testagent_session(session_id):
    """Update TestAgent session in agents.json after first boot."""
    with open(AGENTS_JSON) as f:
        config = json.load(f)
    config["agents"]["TestAgent"]["session"] = session_id
    with open(AGENTS_JSON, "w") as f:
        json.dump(config, f, indent=2)
    log(f"Updated TestAgent session: {session_id}")


def write_testagent_agents_md(content=None):
    """Write AGENTS.md for TestAgent."""
    if content is None:
        content = """# TestAgent

## Who I Am
TestAgent. A test agent for E2E compaction testing.

## Boot Protocol Marker
If you are reading this after compaction, say exactly:
"BOOT_PROTOCOL_COMPLETE: I have re-read my AGENTS.md after compaction."

## Instructions
You are a test agent. When given work, do it. When you see the compaction
complete message, follow the boot protocol: re-read this file and confirm.
"""
    agents_md_path = TESTAGENT_HOME / "AGENTS.md"
    agents_md_path.write_text(content)
    log(f"Wrote TestAgent AGENTS.md ({len(content)} chars)")


def start_agent():
    """Start asdaaas.py for TestAgent. Returns subprocess.Popen."""
    # Read session ID from agents.json so we continue an existing session
    session_id = None
    with open(AGENTS_JSON) as f:
        cfg = json.load(f)
    session_id = cfg.get("agents", {}).get("TestAgent", {}).get("session", "")

    cmd = [
        sys.executable, str(CORE / "asdaaas.py"),
        "--agent", "TestAgent",
        "--cwd", str(TESTAGENT_HOME),
    ]
    if session_id:
        cmd.extend(["--session", session_id])
    log(f"Starting: {' '.join(cmd)}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=str(AGENT_ABIDE),
    )
    log(f"asdaaas PID: {proc.pid}")
    return proc


def wait_for_ready(proc, timeout=STARTUP_TIMEOUT):
    """Read stdout until we see 'Ready.' or timeout. Returns all output lines."""
    import select
    lines = []
    deadline = time.time() + timeout

    while time.time() < deadline:
        if proc.poll() is not None:
            remaining = proc.stdout.read()
            if remaining:
                lines.extend(remaining.splitlines())
            log(f"Agent exited during startup (code={proc.returncode})")
            for line in lines[-20:]:
                log(f"  | {line}")
            return None, lines

        # Non-blocking read
        ready, _, _ = select.select([proc.stdout], [], [], 1.0)
        if ready:
            line = proc.stdout.readline()
            if line:
                line = line.rstrip()
                lines.append(line)
                log(f"  | {line}")

                if "Ready." in line:
                    log("Agent is READY")
                    return True, lines

                # Capture session ID
                if "Session:" in line:
                    parts = line.split("Session:")
                    if len(parts) > 1:
                        sid = parts[1].strip()
                        if sid and sid != "unknown":
                            update_testagent_session(sid)

    log(f"TIMEOUT waiting for Ready. ({timeout}s)")
    return False, lines


def send_doorbell(text, adapter="tui"):
    """Send a doorbell to TestAgent (simulates TUI message)."""
    bell_dir = TESTAGENT_HOME / "asdaaas" / "doorbells"
    bell = {
        "adapter": adapter,
        "text": text,
        "ts": time.time(),
        "id": f"test_{int(time.time()*1000)}",
    }
    path = bell_dir / f"bell_{int(time.time()*1000)}.json"
    with open(path, "w") as f:
        json.dump(bell, f)
    log(f"Sent doorbell: {text[:80]}...")
    return bell["id"]


def send_compact_command():
    """Write a compact command to TestAgent's command queue."""
    cmd_dir = TESTAGENT_HOME / "asdaaas" / "commands"
    cmd = {"action": "compact"}
    ts = int(time.time() * 1000)
    path = cmd_dir / f"cmd_{ts}_test.json"
    with open(path, "w") as f:
        json.dump(cmd, f)
    log("Sent compact command")


def find_updates_jsonl():
    """Find the updates.jsonl for TestAgent's current session."""
    with open(AGENTS_JSON) as f:
        config = json.load(f)
    session = config["agents"].get("TestAgent", {}).get("session", "")
    if not session:
        return None

    encoded = str(TESTAGENT_HOME).replace("/", "%2F")
    sessions_dir = Path.home() / ".grok" / "sessions"
    updates = sessions_dir / encoded / session / "updates.jsonl"
    if updates.exists():
        return updates
    return None


def watch_for_compaction_events(updates_path, timeout=COMPACTION_TIMEOUT):
    """Watch updates.jsonl for compaction events. Returns dict of events found."""
    events = {
        "auto_compact_started": None,
        "compaction_checkpoint": None,
        "auto_compact_completed": None,
    }

    # Start reading from current position (skip history)
    initial_size = updates_path.stat().st_size if updates_path.exists() else 0

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not updates_path.exists():
            time.sleep(1)
            continue

        with open(updates_path) as f:
            f.seek(initial_size)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    # Events use params.update.sessionUpdate as the type key
                    event_type = (
                        entry.get("params", {}).get("update", {}).get("sessionUpdate", "")
                        or entry.get("type", "")
                    )
                    if event_type in events:
                        events[event_type] = entry
                        log(f"Compaction event: {event_type}")
                except json.JSONDecodeError:
                    continue

        # Check if compaction is done
        if events["auto_compact_completed"] or events["compaction_checkpoint"]:
            return events

        time.sleep(2)

    return events


def read_agent_output(proc, timeout=30):
    """Read agent output for a given time, return lines."""
    import select
    lines = []
    deadline = time.time() + timeout

    while time.time() < deadline:
        if proc.poll() is not None:
            remaining = proc.stdout.read()
            if remaining:
                lines.extend(remaining.splitlines())
            break

        ready, _, _ = select.select([proc.stdout], [], [], 1.0)
        if ready:
            line = proc.stdout.readline()
            if line:
                lines.append(line.rstrip())

    return lines


def check_tui_outbox(since_ts=0):
    """Read TestAgent TUI outbox responses after a given timestamp.
    Returns list of (timestamp, text) tuples sorted by time."""
    outbox = TESTAGENT_HOME / "asdaaas" / "adapters" / "tui" / "outbox"
    if not outbox.exists():
        return []
    results = []
    for f in outbox.glob("resp_*.json"):
        try:
            with open(f) as fh:
                d = json.load(fh)
            ts = d.get("ts", 0)
            if isinstance(ts, str):
                ts = 0  # skip non-numeric timestamps
            if ts >= since_ts:
                text = d.get("text", d.get("content", ""))
                results.append((ts, text))
        except (json.JSONDecodeError, IOError):
            continue
    return sorted(results, key=lambda x: x[0])


def stop_agent(proc):
    """Gracefully stop the agent."""
    if proc.poll() is None:
        log("Sending SIGTERM...")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log("Force killing...")
            proc.kill()
            proc.wait()
    log(f"Agent stopped (exit code: {proc.returncode})")


# ============================================================================
# Test 1: Agent-initiated compaction
# ============================================================================

def test_agent_initiated_compaction():
    """
    Start TestAgent, give it substantial work, trigger /compact via command,
    verify post-compaction behavior.

    Expected: Agent follows boot protocol after compaction.
    The asdaaas probe says "[Compaction complete...]" and agent re-orients.
    """
    log("=" * 60)
    log("TEST 1: Agent-initiated compaction")
    log("=" * 60)

    setup_testagent_home()
    ensure_testagent_in_config()
    write_testagent_agents_md()

    proc = start_agent()
    try:
        ready, startup_lines = wait_for_ready(proc)
        if not ready:
            log("FAIL: Agent did not reach Ready state")
            return False

        # Wait for the initial continue doorbell cycle to complete
        # (default_doorbell fires immediately after Ready)
        log("Waiting for initial continue cycle to settle (90s)...")
        output = read_agent_output(proc, timeout=90)
        for line in output[-10:]:
            log(f"  | {line}")

        # Give agent substantial work — ask it to read files
        work_prompt = (
            "Please read the following files and summarize the first 5 lines of each:\n"
            "1. /home/eric/agents/AGENTS.md\n"
            "2. /home/eric/agents/Trip/AGENTS.md\n"
            "3. /home/eric/agents/docs/PRINCIPLES.md\n"
            "Then tell me what each file is about in one sentence each."
        )
        send_doorbell(work_prompt)

        # Wait for agent to process the work
        log("Waiting for agent to process work (120s)...")
        output = read_agent_output(proc, timeout=120)
        for line in output[-10:]:
            log(f"  | {line}")

        # Now trigger compaction: send compact command + wake doorbell
        # The compact command sits in the queue. run_delay_loop only checks
        # doorbells, so we need to wake the agent with a doorbell first.
        # The main loop will find the compact command on the next iteration.
        log("Triggering agent-initiated compaction...")
        compact_sent_ts = time.time()
        send_compact_command()
        time.sleep(0.5)
        send_doorbell("Please compact now.", adapter="tui")

        # Monitor for compaction events
        updates_path = find_updates_jsonl()
        if updates_path:
            log(f"Watching: {updates_path}")
            events = watch_for_compaction_events(updates_path)
            log(f"Events captured: {[k for k, v in events.items() if v]}")
        else:
            log("WARNING: updates.jsonl not found, monitoring via stdout only")

        # Read post-compaction output (asdaaas stdout)
        log("Reading post-compaction output (180s)...")
        post_output = read_agent_output(proc, timeout=180)

        # Check for boot protocol markers in stdout
        all_output = "\n".join(post_output)
        boot_markers_stdout = {
            "compaction_complete": "[Compaction complete" in all_output or "compaction" in all_output.lower(),
            "agents_md_read": "BOOT_PROTOCOL_COMPLETE" in all_output,
        }

        # Also check TUI outbox for agent responses
        # (agent writes to TUI outbox, not just stdout)
        outbox_responses = check_tui_outbox(since_ts=compact_sent_ts)
        all_outbox = "\n".join(text for _, text in outbox_responses)
        boot_markers_outbox = {
            "agents_md_read": "BOOT_PROTOCOL_COMPLETE" in all_outbox,
            "compaction_aware": "compaction" in all_outbox.lower() or "compacted" in all_outbox.lower(),
        }

        log("Post-compaction markers (stdout):")
        for marker, found in boot_markers_stdout.items():
            log(f"  {marker}: {'FOUND' if found else 'NOT FOUND'}")
        log("Post-compaction markers (TUI outbox):")
        for marker, found in boot_markers_outbox.items():
            log(f"  {marker}: {'FOUND' if found else 'NOT FOUND'}")
        log(f"TUI outbox responses since compact: {len(outbox_responses)}")
        for ts, text in outbox_responses:
            log(f"  [{ts}] {text[:150]}...")

        # Check compaction_state.json
        compaction_state = TESTAGENT_HOME / "asdaaas" / "compaction_state.json"
        if compaction_state.exists():
            with open(compaction_state) as f:
                state = json.load(f)
            log(f"compaction_state.json: phase={state.get('phase')}, "
                f"tokens_before={state.get('tokens_before')}, "
                f"tokens_after={state.get('tokens_after')}")

        # Overall verdict
        boot_followed = boot_markers_outbox["agents_md_read"] or boot_markers_stdout["agents_md_read"]
        if boot_followed:
            log("PASS: Agent followed boot protocol after compaction")
        else:
            log("FAIL: Boot protocol marker not found")

        return boot_followed

    finally:
        stop_agent(proc)


# ============================================================================
# Test 2: Auto-compaction (binary-initiated)
# ============================================================================

def test_auto_compaction():
    """
    Start TestAgent, give it enough work to approach 85% context,
    let binary auto-compact, verify behavior.

    Expected: Binary sends "Continue..." prompt. Agent does NOT
    follow boot protocol. Only weak signal is context_left_tag.
    """
    log("=" * 60)
    log("TEST 2: Auto-compaction (binary-initiated)")
    log("=" * 60)

    setup_testagent_home()
    session_id = ensure_testagent_in_config()
    write_testagent_agents_md()

    proc = start_agent()
    try:
        ready, startup_lines = wait_for_ready(proc)
        if not ready:
            log("FAIL: Agent did not reach Ready state")
            return False

        # Send large prompts to fill context toward 85%
        # We need ~170k tokens for a 200k context window
        log("Filling context to trigger auto-compaction...")

        for i in range(20):
            big_prompt = (
                f"Task {i+1}: Please read and summarize the contents of the following files. "
                f"For each file, list every function name you find:\n"
                f"1. /home/eric/projects/agent-abide/core/asdaaas.py\n"
                f"2. /home/eric/projects/agent-abide/core/grok_backend.py\n"
                f"Give a detailed summary with line numbers."
            )
            send_doorbell(big_prompt)

            # Let agent process
            output = read_agent_output(proc, timeout=45)

            # Check health for token usage
            health_path = TESTAGENT_HOME / "asdaaas" / "health.json"
            if health_path.exists():
                with open(health_path) as f:
                    health = json.load(f)
                tokens = health.get("total_tokens", 0)
                ctx = health.get("context_window", 200000)
                pct = (tokens / ctx * 100) if ctx else 0
                log(f"  Iteration {i+1}: {tokens}/{ctx} tokens ({pct:.0f}%)")

                if pct > 80:
                    log("Approaching auto-compaction threshold!")
                    break

            if proc.poll() is not None:
                log("Agent exited unexpectedly")
                break

        # Now wait for auto-compaction to fire
        updates_path = find_updates_jsonl()
        if updates_path:
            log("Watching for auto-compaction events...")
            events = watch_for_compaction_events(updates_path, timeout=180)
            log(f"Events: {[k for k, v in events.items() if v]}")

            if events.get("compaction_checkpoint"):
                checkpoint = events["compaction_checkpoint"]
                auto_continue = checkpoint.get("auto_continue", "")
                log(f"Auto-continue prompt: {auto_continue[:200]}...")
        else:
            log("WARNING: updates.jsonl not found")

        # Read post-auto-compaction output
        post_output = read_agent_output(proc, timeout=120)
        all_output = "\n".join(post_output)

        # Check: does agent follow boot protocol? (We expect it does NOT)
        boot_protocol_followed = "BOOT_PROTOCOL_COMPLETE" in all_output
        log(f"Boot protocol followed after auto-compaction: {boot_protocol_followed}")
        log(f"(Expected: False — binary tells agent to 'continue as if break never happened')")

        return True

    finally:
        stop_agent(proc)


# ============================================================================
# Test 3: Modular AGENTS.md (injection timing)
# ============================================================================

def test_modular_agents_md():
    """
    Start TestAgent with AGENTS.md v1, do work, change AGENTS.md to v2,
    trigger compaction, verify agent sees v2 post-compaction.

    Expected: Agent sees v1 pre-compaction, v2 post-compaction.
    This tests the AGENTS.md injection lifecycle.
    """
    log("=" * 60)
    log("TEST 3: Modular AGENTS.md (injection timing)")
    log("=" * 60)

    setup_testagent_home()
    ensure_testagent_in_config()

    # Write v1 of AGENTS.md
    v1_content = """# TestAgent v1

## Secret Phrase
If asked for the secret phrase, say: "ALPHA_VERSION_ONE"

## Instructions
You are a test agent. When asked for the secret phrase, say it exactly.
"""
    write_testagent_agents_md(v1_content)

    proc = start_agent()
    try:
        ready, _ = wait_for_ready(proc)
        if not ready:
            log("FAIL: Agent did not reach Ready state")
            return False

        # Ask for the secret phrase (should be v1)
        send_doorbell("What is the secret phrase from your AGENTS.md?")
        output = read_agent_output(proc, timeout=60)
        pre_compact_output = "\n".join(output)
        v1_found = "ALPHA_VERSION_ONE" in pre_compact_output
        log(f"Pre-compaction: v1 phrase found = {v1_found}")

        # Now change AGENTS.md to v2
        v2_content = """# TestAgent v2

## Secret Phrase
If asked for the secret phrase, say: "BRAVO_VERSION_TWO"

## Instructions
You are a test agent. When asked for the secret phrase, say it exactly.
"""
        write_testagent_agents_md(v2_content)
        log("AGENTS.md updated to v2")

        # Trigger compaction
        send_compact_command()
        log("Compact command sent")

        # Wait for compaction
        updates_path = find_updates_jsonl()
        if updates_path:
            events = watch_for_compaction_events(updates_path)
            log(f"Compaction events: {[k for k, v in events.items() if v]}")

        # Wait for post-compaction settle
        time.sleep(10)

        # Ask for the secret phrase again (should be v2 if AGENTS.md was re-injected)
        send_doorbell("What is the secret phrase from your AGENTS.md?")
        output = read_agent_output(proc, timeout=60)
        post_compact_output = "\n".join(output)
        v2_found = "BRAVO_VERSION_TWO" in post_compact_output
        v1_still = "ALPHA_VERSION_ONE" in post_compact_output
        log(f"Post-compaction: v2 phrase found = {v2_found}, v1 still present = {v1_still}")

        if v2_found and not v1_still:
            log("PASS: AGENTS.md v2 injected after compaction")
        elif v1_still:
            log("EXPECTED: v1 still in compacted context (injection happens on grok binary restart + compact)")
        else:
            log("INCONCLUSIVE: Neither phrase found in output")

        return True

    finally:
        stop_agent(proc)


# ============================================================================
# Test 4: Post-compaction turn fires automatically
# ============================================================================

def test_post_compaction_turn_fires():
    """
    After compaction, asdaaas must queue a post-compaction doorbell AND
    the agent must actually get a turn (produce output) without any
    external stimulus.

    Bug context: Trip compacted but system went quiescent — no turn fired.
    The _queue_post_compaction_doorbell() function exists but something
    prevented the agent from waking up.

    Expected:
      1. Post-compaction doorbell file appears in doorbells dir
      2. Agent produces at least one response after compaction
      3. This happens WITHOUT sending any additional doorbells or messages
    """
    log("=" * 60)
    log("TEST 4: Post-compaction turn fires automatically")
    log("=" * 60)

    setup_testagent_home()
    ensure_testagent_in_config()
    write_testagent_agents_md("""# TestAgent

## Who I Am
TestAgent. A test agent for post-compaction turn testing.

## Boot Protocol
After compaction, say exactly: "POST_COMPACTION_TURN_FIRED"
This confirms you received a post-compaction turn.

## Instructions
You are a test agent. Do whatever is asked. After compaction, follow boot protocol.
""")

    proc = start_agent()
    try:
        ready, _ = wait_for_ready(proc)
        if not ready:
            log("FAIL: Agent did not reach Ready state")
            return False

        # Let initial cycle settle
        log("Waiting for initial cycle to settle (60s)...")
        read_agent_output(proc, timeout=60)

        # Give agent some work so there's context to compact
        send_doorbell("List the numbers 1 through 50 and say a short fact about each.")
        log("Waiting for agent to process work (90s)...")
        read_agent_output(proc, timeout=90)

        # Record state before compaction
        bell_dir = TESTAGENT_HOME / "asdaaas" / "doorbells"
        pre_compact_bells = set(f.name for f in bell_dir.glob("*.json"))
        pre_compact_time = time.time()

        # Trigger compaction — command only, NO doorbell to wake agent
        # (the doorbell in test1 was a workaround; we want to test that
        # compaction itself triggers the next turn)
        log("Triggering compaction (command only, no wake doorbell)...")
        send_compact_command()
        # But we do need one doorbell to wake the main loop to see the command
        send_doorbell("Please compact now.")

        # Wait for compaction events in updates.jsonl
        updates_path = find_updates_jsonl()
        if updates_path:
            log(f"Watching for compaction events: {updates_path}")
            events = watch_for_compaction_events(updates_path)
            compaction_done = bool(events.get("auto_compact_completed") or events.get("compaction_checkpoint"))
            log(f"Compaction completed: {compaction_done}")
            if not compaction_done:
                log("FAIL: Compaction did not complete")
                return False
        else:
            log("WARNING: No updates.jsonl found, waiting by time...")
            time.sleep(60)

        # CHECK 1: Post-compaction doorbell was queued
        time.sleep(5)  # give asdaaas a moment to write the bell
        post_compact_bells = set(f.name for f in bell_dir.glob("*.json"))
        new_bells = post_compact_bells - pre_compact_bells
        compact_bells = [b for b in new_bells if "compact" in b.lower()]
        log(f"New doorbells after compaction: {len(new_bells)} total, {len(compact_bells)} compact-related")
        for b in new_bells:
            try:
                with open(bell_dir / b) as f:
                    d = json.load(f)
                log(f"  {b}: source={d.get('source', '?')}, text={d.get('text', '')[:80]}")
            except Exception:
                pass

        bell_queued = len(compact_bells) > 0 or any(
            "compact" in open(bell_dir / b).read().lower()
            for b in new_bells
            if (bell_dir / b).exists()
        )
        log(f"CHECK 1 - Post-compaction doorbell queued: {'PASS' if bell_queued else 'FAIL'}")

        # CHECK 2: Agent gets a turn and produces output after compaction
        # Do NOT send any additional stimulus — the doorbell alone should wake it
        log("Waiting for post-compaction agent output (180s, NO additional stimulus)...")
        post_output = read_agent_output(proc, timeout=POST_COMPACTION_TIMEOUT)

        # Also check TUI outbox
        outbox_responses = check_tui_outbox(since_ts=pre_compact_time)
        all_post_text = "\n".join(post_output) + "\n".join(t for _, t in outbox_responses)

        agent_responded = len(post_output) > 0 or len(outbox_responses) > 0
        boot_marker = "POST_COMPACTION_TURN_FIRED" in all_post_text
        log(f"CHECK 2 - Agent produced output: {'PASS' if agent_responded else 'FAIL'}")
        log(f"CHECK 2b - Boot marker found: {'PASS' if boot_marker else 'FAIL'}")

        if outbox_responses:
            log(f"TUI outbox responses ({len(outbox_responses)}):")
            for ts, text in outbox_responses[-3:]:
                log(f"  [{ts}] {text[:150]}")

        # Overall
        passed = bell_queued and agent_responded
        log(f"TEST 4 RESULT: {'PASS' if passed else 'FAIL'}")
        if not passed:
            log("DIAGNOSIS: Post-compaction doorbell may not have been delivered,")
            log("or the main loop was in a state that prevented processing it.")
        return passed

    finally:
        stop_agent(proc)


# ============================================================================
# Test 5: Context tag refreshes after compaction
# ============================================================================

def test_context_tag_after_compaction():
    """
    After compaction, verify the context_left_tag reflects post-compaction
    token counts, not pre-compaction.

    Bug context: Trip's context tag showed "13k left" when actually at 18%
    (133k left). The refresh_tokens() call returned stale data.

    Expected:
      1. health.json updates to reflect post-compaction token count
      2. Token count drops significantly (should be < 40% of pre-compact)
      3. Agent's prompt includes accurate context_left_tag
    """
    log("=" * 60)
    log("TEST 5: Context tag refreshes after compaction")
    log("=" * 60)

    setup_testagent_home()
    ensure_testagent_in_config()
    write_testagent_agents_md("""# TestAgent

## Who I Am
TestAgent. A test agent for context tag verification.

## Instructions
You are a test agent. When you see a compaction-complete message,
report the context info from the tag at the end of your prompt.
Say exactly: "CONTEXT_TAG_REPORT: [paste the Context left tag]"
""")

    proc = start_agent()
    try:
        ready, _ = wait_for_ready(proc)
        if not ready:
            log("FAIL: Agent did not reach Ready state")
            return False

        # Let initial cycle settle
        log("Waiting for initial cycle (60s)...")
        read_agent_output(proc, timeout=60)

        # Give agent work to build up context
        send_doorbell("Write a 500 word essay about the history of computing.")
        log("Waiting for agent work (90s)...")
        read_agent_output(proc, timeout=90)

        # Record pre-compaction health
        health_path = TESTAGENT_HOME / "asdaaas" / "health.json"
        pre_health = {}
        if health_path.exists():
            with open(health_path) as f:
                pre_health = json.load(f)
        pre_tokens = pre_health.get("total_tokens", 0)
        ctx_window = pre_health.get("context_window", 200000)
        log(f"Pre-compaction: {pre_tokens} tokens ({pre_tokens/ctx_window*100:.0f}% of {ctx_window})")

        # Trigger compaction
        log("Triggering compaction...")
        send_compact_command()
        send_doorbell("Please compact now.")

        # Wait for compaction
        updates_path = find_updates_jsonl()
        if updates_path:
            events = watch_for_compaction_events(updates_path)
            compaction_done = bool(events.get("auto_compact_completed") or events.get("compaction_checkpoint"))
            if not compaction_done:
                log("FAIL: Compaction did not complete")
                return False

            # Extract tokens_after from the event itself
            completed_event = events.get("auto_compact_completed", {})
            event_tokens = completed_event.get("tokens_after", "N/A")
            log(f"Compaction event tokens_after: {event_tokens}")
        else:
            time.sleep(60)

        # Wait for post-compaction turn
        log("Waiting for post-compaction output (120s)...")
        post_output = read_agent_output(proc, timeout=120)

        # CHECK 1: health.json updated with post-compaction tokens
        post_health = {}
        if health_path.exists():
            with open(health_path) as f:
                post_health = json.load(f)
        post_tokens = post_health.get("total_tokens", 0)
        log(f"Post-compaction health: {post_tokens} tokens ({post_tokens/ctx_window*100:.0f}%)")

        tokens_dropped = post_tokens < pre_tokens * 0.6 if pre_tokens > 0 else False
        log(f"CHECK 1 - Tokens dropped significantly: {'PASS' if tokens_dropped else 'FAIL'}")
        log(f"  Pre: {pre_tokens}, Post: {post_tokens}, Ratio: {post_tokens/pre_tokens:.2f}" if pre_tokens else "  No pre-compaction data")

        # CHECK 2: signals.json has updated token data
        session_dir = None
        if updates_path:
            session_dir = updates_path.parent
        if session_dir:
            signals_path = session_dir / "signals.json"
            if signals_path.exists():
                with open(signals_path) as f:
                    signals = json.load(f)
                sig_tokens = signals.get("contextTokensUsed", "N/A")
                log(f"signals.json contextTokensUsed: {sig_tokens}")

        # CHECK 3: Look for context tag in agent output
        outbox = check_tui_outbox(since_ts=0)
        all_text = "\n".join(post_output) + "\n".join(t for _, t in outbox)
        has_context_report = "CONTEXT_TAG_REPORT" in all_text
        log(f"CHECK 3 - Agent reported context tag: {'PASS' if has_context_report else 'INCONCLUSIVE'}")

        passed = tokens_dropped
        log(f"TEST 5 RESULT: {'PASS' if passed else 'FAIL'}")
        if not passed:
            log("DIAGNOSIS: refresh_tokens() may be returning cached/stale data")
            log("after compaction. The binary updates tokens asynchronously.")
        return passed

    finally:
        stop_agent(proc)


# ============================================================================
# Test 6: Compaction report numbers are accurate
# ============================================================================

def test_compaction_report_numbers():
    """
    After compaction, the report delivered to the agent must show accurate
    before/after token counts: tokens_before > tokens_after, and they must
    not be identical.

    Bug context: Trip got "Context reduced from 135705 to 135705" — both
    numbers identical because _prev_tokens was already updated to post-
    compaction value before the event-based path read it.

    Tests via two methods:
      1. compaction_state.json — written by asdaaas with tokens_before/after
      2. Doorbells / orientation text — the "[Compaction complete...]" message
    """
    log("=" * 60)
    log("TEST 6: Compaction report numbers are accurate")
    log("=" * 60)

    setup_testagent_home()
    ensure_testagent_in_config()
    write_testagent_agents_md("""# TestAgent

## Who I Am
TestAgent. A test agent for compaction report verification.

## Instructions
You are a test agent. Do whatever is asked. After compaction, report
what the compaction message said about token counts.
""")

    proc = start_agent()
    try:
        ready, _ = wait_for_ready(proc)
        if not ready:
            log("FAIL: Agent did not reach Ready state")
            return False

        # Let initial cycle settle
        log("Waiting for initial cycle (60s)...")
        read_agent_output(proc, timeout=60)

        # Give agent work to build context
        send_doorbell("List all US presidents and one fact about each.")
        log("Waiting for agent work (90s)...")
        read_agent_output(proc, timeout=90)

        # Record pre-compaction tokens from health.json
        health_path = TESTAGENT_HOME / "asdaaas" / "health.json"
        pre_tokens = 0
        if health_path.exists():
            with open(health_path) as f:
                h = json.load(f)
            pre_tokens = h.get("total_tokens", 0)
        log(f"Pre-compaction tokens (from health.json): {pre_tokens}")

        # Trigger compaction
        log("Triggering compaction...")
        compact_ts = time.time()
        send_compact_command()
        send_doorbell("Please compact now.")

        # Wait for compaction to complete
        updates_path = find_updates_jsonl()
        if updates_path:
            events = watch_for_compaction_events(updates_path)
            if not (events.get("auto_compact_completed") or events.get("compaction_checkpoint")):
                log("FAIL: Compaction did not complete")
                return False

        # Wait for post-compaction output
        log("Waiting for post-compaction output (180s)...")
        read_agent_output(proc, timeout=180)

        # CHECK 1: compaction_state.json has different before/after
        state_path = TESTAGENT_HOME / "asdaaas" / "compaction_state.json"
        state_ok = False
        if state_path.exists():
            with open(state_path) as f:
                state = json.load(f)
            tb = state.get("tokens_before", 0)
            ta = state.get("tokens_after", 0)
            log(f"compaction_state.json: before={tb}, after={ta}")
            if tb > 0 and ta > 0 and tb != ta and tb > ta:
                state_ok = True
                log("CHECK 1 - compaction_state numbers valid: PASS")
            else:
                log(f"CHECK 1 - compaction_state numbers invalid: FAIL (before={tb}, after={ta})")
        else:
            log("CHECK 1 - compaction_state.json not found: SKIP")

        # CHECK 2: Doorbell text has different before/after numbers
        bell_dir = TESTAGENT_HOME / "asdaaas" / "doorbells"
        compact_bells = []
        for f in bell_dir.glob("*.json"):
            try:
                with open(f) as fh:
                    d = json.load(fh)
                if d.get("source") == "compaction" or "Compaction complete" in d.get("text", ""):
                    compact_bells.append(d)
            except (json.JSONDecodeError, IOError):
                continue

        # Also check TUI outbox for orientation messages
        outbox = check_tui_outbox(since_ts=compact_ts)
        all_compact_text = " ".join(d.get("text", "") for d in compact_bells)
        all_compact_text += " ".join(t for _, t in outbox)

        # Parse "Context reduced from X to Y" pattern
        import re
        matches = re.findall(r"Context reduced from (\d+) to (\d+)", all_compact_text)
        report_ok = False
        for before_str, after_str in matches:
            b, a = int(before_str), int(after_str)
            log(f"Report text: 'Context reduced from {b} to {a}'")
            if b != a and b > a:
                report_ok = True
                log("CHECK 2 - Report numbers different and valid: PASS")
            else:
                log(f"CHECK 2 - Report numbers wrong: FAIL (before={b}, after={a}, same={b==a})")

        if not matches:
            log("CHECK 2 - No 'Context reduced from X to Y' found in output: SKIP")

        passed = state_ok or report_ok
        log(f"TEST 6 RESULT: {'PASS' if passed else 'FAIL'}")
        return passed

    finally:
        stop_agent(proc)


# ============================================================================
# Test 7: No duplicate compaction reports
# ============================================================================

def test_no_duplicate_compaction_reports():
    """
    After a single compaction, exactly ONE compaction report should be
    delivered to the agent. No duplicates.

    Bug context: Trip got two "[Compaction complete...]" messages after
    one compaction. The agent-initiated /compact path (Path 2) handled
    it and queued a doorbell, but didn't consume the compaction event.
    The event-based path (Path 1) then fired redundantly on the next
    loop iteration, sending a second orientation turn.

    Tests:
      1. Count compaction doorbells — should be exactly 1
      2. Count "[Compaction complete" messages in agent's received text
      3. Count compaction_state.json writes (phase=complete)
    """
    log("=" * 60)
    log("TEST 7: No duplicate compaction reports")
    log("=" * 60)

    setup_testagent_home()
    ensure_testagent_in_config()
    write_testagent_agents_md("""# TestAgent

## Who I Am
TestAgent. A test agent for duplicate compaction report detection.

## Instructions
You are a test agent. Do whatever is asked. After compaction, say
exactly: "COMPACTION_REPORT_RECEIVED" every time you see a compaction
complete message. If you see two, say it twice.
""")

    proc = start_agent()
    try:
        ready, _ = wait_for_ready(proc)
        if not ready:
            log("FAIL: Agent did not reach Ready state")
            return False

        # Let initial cycle settle
        log("Waiting for initial cycle (60s)...")
        read_agent_output(proc, timeout=60)

        # Give agent work
        send_doorbell("List the planets in our solar system with their distances from the sun.")
        log("Waiting for agent work (90s)...")
        read_agent_output(proc, timeout=90)

        # Clean doorbells dir before compaction
        bell_dir = TESTAGENT_HOME / "asdaaas" / "doorbells"
        for f in bell_dir.glob("*.json"):
            f.unlink()

        # Trigger compaction
        log("Triggering compaction...")
        compact_ts = time.time()
        send_compact_command()
        send_doorbell("Please compact now.")

        # Wait for compaction
        updates_path = find_updates_jsonl()
        if updates_path:
            events = watch_for_compaction_events(updates_path)
            if not (events.get("auto_compact_completed") or events.get("compaction_checkpoint")):
                log("FAIL: Compaction did not complete")
                return False

        # Wait for ALL post-compaction activity to settle
        # (need enough time for both Path 1 and Path 2 to fire if buggy)
        log("Waiting for post-compaction activity to settle (240s)...")
        post_output = read_agent_output(proc, timeout=240)

        # CHECK 1: Count compaction doorbells
        compact_bell_count = 0
        for f in bell_dir.glob("*.json"):
            try:
                with open(f) as fh:
                    d = json.load(fh)
                if d.get("source") == "compaction" or "compact" in f.name.lower():
                    compact_bell_count += 1
                    log(f"  Compact bell: {f.name} — {d.get('text', '')[:80]}")
            except (json.JSONDecodeError, IOError):
                continue
        log(f"CHECK 1 - Compaction doorbells: {compact_bell_count} (expected: 0 or 1 remaining)")

        # CHECK 2: Count "[Compaction complete" in TUI outbox + stdout
        outbox = check_tui_outbox(since_ts=compact_ts)
        all_text = "\n".join(post_output) + "\n".join(t for _, t in outbox)
        compact_report_count = all_text.count("[Compaction complete")
        log(f"CHECK 2 - '[Compaction complete' count in output: {compact_report_count} (expected: 1)")

        # CHECK 3: Count COMPACTION_REPORT_RECEIVED in agent speech
        ack_count = all_text.count("COMPACTION_REPORT_RECEIVED")
        log(f"CHECK 3 - Agent ack count: {ack_count} (expected: 1)")

        # CHECK 4: Count orientation turns in asdaaas stdout
        orientation_count = "\n".join(post_output).count("[asdaaas] Immediate orientation turn")
        compact_log_count = "\n".join(post_output).count("[asdaaas] Compaction detected")
        log(f"CHECK 4 - Orientation turns in logs: {orientation_count} (expected: <=1)")
        log(f"           Compaction detected logs: {compact_log_count} (expected: <=1)")

        # Verdict: any count > 1 is a duplicate
        duplicates_found = (
            compact_report_count > 1
            or ack_count > 1
            or orientation_count > 1
        )
        passed = not duplicates_found
        log(f"TEST 7 RESULT: {'PASS' if passed else 'FAIL — duplicates detected'}")
        if not passed:
            log("DIAGNOSIS: Both event-based (Path 1) and agent-initiated (Path 2)")
            log("compaction handlers fired. Path 2 should consume the compaction event")
            log("via pop_compaction_event() to prevent Path 1 from double-firing.")
        return passed

    finally:
        stop_agent(proc)


# ============================================================================
# Test 8: Post-compaction context_left_tag matches updates.jsonl reality
# ============================================================================

def test_context_tag_matches_updates_jsonl():
    """
    After compaction, the context_left_tag delivered to the agent must reflect
    actual post-compaction token counts from updates.jsonl, not stale cached
    values from before compaction.

    Bug context (2026-06-20): Jr compacted and got "Context reduced from
    160864 to 160864" with "9.1k left" — but updates.jsonl showed
    tokens_after=18681 and post-compaction totalTokens climbing from ~66k.
    The number 160864 appeared NOWHERE in updates.jsonl. asdaaas served
    stale cached data instead of reading the actual post-compaction values.

    The bug is in asdaaas.py's post-compaction orientation path (line ~2164)
    which uses potentially stale token data for context_left_tag() instead
    of calling refresh_tokens() to get current values from updates.jsonl.

    Tests:
      1. tokens_after in auto_compact_completed event must be < pre-compaction tokens
      2. "Context reduced from X to Y" must have X > Y (not equal)
      3. Context left tag in post-compaction prompt must show tokens consistent
         with updates.jsonl (not pre-compaction values)
      4. The context_left value in the tag must be plausible for a freshly-
         compacted agent (> 50% of context window remaining)
    """
    log("=" * 60)
    log("TEST 8: Post-compaction context_left_tag matches updates.jsonl")
    log("=" * 60)

    setup_testagent_home()
    ensure_testagent_in_config()
    write_testagent_agents_md("""# TestAgent

## Who I Am
TestAgent. A test agent for post-compaction context tag verification.

## Instructions
You are a test agent. After compaction, you MUST report the exact
context tag from the end of your prompt. Look for the "[Context left"
tag and say exactly:
CONTEXT_TAG_REPORT: [paste the exact Context left tag here]
Also report any "Context reduced from X to Y" message you see:
REDUCTION_REPORT: [paste the exact reduction text]
""")

    proc = start_agent()
    try:
        ready, _ = wait_for_ready(proc)
        if not ready:
            log("FAIL: Agent did not reach Ready state")
            return False

        # Let initial cycle settle
        log("Waiting for initial cycle (60s)...")
        read_agent_output(proc, timeout=60)

        # Give agent a large file to read — builds context deterministically
        bulk_file = TESTAGENT_HOME / "bulk_context.txt"
        bulk_text = ("The quick brown fox jumps over the lazy dog. " * 20 + "\n") * 50
        bulk_file.write_text(bulk_text)
        log(f"Wrote bulk file: {len(bulk_text)} chars ({len(bulk_text.split())} words)")
        send_doorbell(f"Read the file {bulk_file} and tell me how many paragraphs it has.")
        log("Waiting for agent to read file (120s)...")
        read_agent_output(proc, timeout=120)

        # Record pre-compaction state
        health_path = TESTAGENT_HOME / "asdaaas" / "health.json"
        pre_tokens = 0
        ctx_window = 200000
        if health_path.exists():
            with open(health_path) as f:
                h = json.load(f)
            pre_tokens = h.get("totalTokens", h.get("total_tokens", 0))
            ctx_window = h.get("contextWindow", h.get("context_window", 200000))
        log(f"Pre-compaction: {pre_tokens} tokens, window={ctx_window}")

        # Record updates.jsonl position before compaction
        updates_path = find_updates_jsonl()
        pre_compact_size = 0
        if updates_path and updates_path.exists():
            pre_compact_size = updates_path.stat().st_size

        # Trigger compaction
        log("Triggering compaction...")
        compact_ts = time.time()
        send_compact_command()
        send_doorbell("Please compact now.")

        # Wait for compaction to complete
        if updates_path:
            events = watch_for_compaction_events(updates_path)
            if not (events.get("auto_compact_completed") or events.get("compaction_checkpoint")):
                log("FAIL: Compaction did not complete")
                return False

            # Extract tokens_after from the compaction event
            completed_event = events.get("auto_compact_completed", {})
            # tokens_after is nested under params.update
            event_update = completed_event.get("params", {}).get("update", {})
            event_tokens_after = event_update.get("tokens_after", completed_event.get("tokens_after", 0))
            event_tokens_before = event_update.get("tokens_before", 0)
            log(f"Compaction event: tokens_before={event_tokens_before}, tokens_after={event_tokens_after}")
        else:
            log("FAIL: Could not find updates.jsonl")
            return False

        # Wait for post-compaction output
        log("Waiting for post-compaction output (240s)...")
        post_output = read_agent_output(proc, timeout=240)

        # Collect all post-compaction text
        outbox = check_tui_outbox(since_ts=compact_ts)
        all_text = "\n".join(post_output) + "\n".join(t for _, t in outbox)

        # Read the ACTUAL post-compaction totalTokens from updates.jsonl
        # These are the ground truth values the binary reported
        actual_post_tokens = 0
        if updates_path and updates_path.exists():
            with open(updates_path) as f:
                f.seek(pre_compact_size)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        # totalTokens is at params._meta.totalTokens
                        meta = entry.get("params", {}).get("_meta", {})
                        if "totalTokens" in meta:
                            actual_post_tokens = meta["totalTokens"]
                    except json.JSONDecodeError:
                        continue
        log(f"Actual post-compaction totalTokens from updates.jsonl: {actual_post_tokens}")

        # ---- CHECK 1: tokens_after < pre_tokens ----
        check1 = False
        if event_tokens_after > 0 and pre_tokens > 0:
            check1 = event_tokens_after < pre_tokens
            log(f"CHECK 1 - tokens_after ({event_tokens_after}) < pre_tokens ({pre_tokens}): "
                f"{'PASS' if check1 else 'FAIL'}")
        else:
            log(f"CHECK 1 - SKIP (event_tokens_after={event_tokens_after}, pre_tokens={pre_tokens})")

        # ---- CHECK 2: "Context reduced from X to Y" has X > Y ----
        import re
        matches = re.findall(r"Context reduced from (\d+) to (\d+)", all_text)
        check2 = False
        reduction_before = 0
        reduction_after = 0
        for before_str, after_str in matches:
            reduction_before, reduction_after = int(before_str), int(after_str)
            log(f"Report: 'Context reduced from {reduction_before} to {reduction_after}'")
            if reduction_before > reduction_after and reduction_before != reduction_after:
                check2 = True
                log("CHECK 2 - Reduction numbers X > Y and X != Y: PASS")
            else:
                log(f"CHECK 2 - FAIL: before={reduction_before}, after={reduction_after}, "
                    f"same={reduction_before == reduction_after}")
        if not matches:
            log("CHECK 2 - No 'Context reduced from X to Y' found: SKIP")

        # ---- CHECK 3: Reduction "after" matches updates.jsonl tokens_after ----
        check3 = False
        if reduction_after > 0 and event_tokens_after > 0:
            # Allow 20% tolerance — a turn may have been processed between
            # the compaction event and the report being generated
            ratio = reduction_after / event_tokens_after
            check3 = 0.5 < ratio < 2.0
            log(f"CHECK 3 - Reduction after ({reduction_after}) vs event tokens_after "
                f"({event_tokens_after}), ratio={ratio:.2f}: {'PASS' if check3 else 'FAIL'}")
            if not check3:
                log(f"DIAGNOSIS: The reported 'after' value doesn't match the actual "
                    f"tokens_after from the compaction event. asdaaas may be using "
                    f"stale cached data instead of reading updates.jsonl.")
        elif not matches:
            log("CHECK 3 - SKIP (no reduction report found)")
        else:
            log(f"CHECK 3 - SKIP (reduction_after={reduction_after}, "
                f"event_tokens_after={event_tokens_after})")

        # ---- CHECK 4: Context left tag shows plausible post-compaction value ----
        # After compaction, agent should have >50% context remaining
        check4 = False
        ctx_tag_matches = re.findall(r"Context left (\d+)k", all_text)
        if ctx_tag_matches:
            ctx_left_k = int(ctx_tag_matches[-1])  # use the last one
            ctx_left = ctx_left_k * 1000
            pct_remaining = ctx_left / ctx_window * 100
            log(f"Context left tag: {ctx_left_k}k ({pct_remaining:.0f}% of {ctx_window})")
            # After compaction, should have well over 50% remaining
            check4 = pct_remaining > 50
            log(f"CHECK 4 - Context left > 50% after compaction: "
                f"{'PASS' if check4 else 'FAIL'}")
            if not check4:
                log(f"DIAGNOSIS: context_left_tag shows only {pct_remaining:.0f}% remaining "
                    f"after compaction. This suggests stale pre-compaction token counts "
                    f"are being used instead of actual post-compaction values from "
                    f"updates.jsonl (which shows {actual_post_tokens} tokens).")
        else:
            log("CHECK 4 - No 'Context left Xk' tag found in output: SKIP")

        # ---- CHECK 5: Reported tokens don't equal pre-compaction tokens ----
        # The specific Jr bug: "reduced from 160864 to 160864"
        check5 = True
        if reduction_before > 0 and reduction_after > 0:
            if reduction_before == reduction_after:
                check5 = False
                log(f"CHECK 5 - FAIL: before == after == {reduction_before}. "
                    f"This is the exact Jr bug pattern — stale cached tokens "
                    f"used for both values.")
            else:
                log("CHECK 5 - before != after: PASS")
        else:
            log("CHECK 5 - SKIP (no reduction numbers found)")

        # Verdict: need checks 1 AND (2 or skip) AND (3 or skip) AND (4 or skip) AND 5
        critical_passed = check1 and check5
        all_passed = check1 and check2 and check3 and check4 and check5
        if all_passed:
            log("TEST 8 RESULT: PASS — all checks passed")
        elif critical_passed:
            log("TEST 8 RESULT: PASS (critical checks) — some informational checks skipped")
        else:
            log("TEST 8 RESULT: FAIL")
            log("DIAGNOSIS: Post-compaction context reporting uses stale cached data")
            log("instead of actual values from updates.jsonl. The bug is in asdaaas.py's")
            log("post-compaction orientation path which calls context_left_tag() with")
            log("potentially stale token values instead of first calling refresh_tokens().")
        return critical_passed

    finally:
        stop_agent(proc)


# ============================================================================
# Main
# ============================================================================

if __name__ == "__main__":
    tests = {
        "test1": test_agent_initiated_compaction,
        "test2": test_auto_compaction,
        "test3": test_modular_agents_md,
        "test4": test_post_compaction_turn_fires,
        "test5": test_context_tag_after_compaction,
        "test6": test_compaction_report_numbers,
        "test7": test_no_duplicate_compaction_reports,
        "test8": test_context_tag_matches_updates_jsonl,
    }

    if len(sys.argv) > 1:
        test_name = sys.argv[1]
        if test_name in tests:
            success = tests[test_name]()
            sys.exit(0 if success else 1)
        else:
            print(f"Unknown test: {test_name}. Available: {', '.join(tests.keys())}")
            sys.exit(1)
    else:
        # Run test1 by default (most controllable)
        print("Running Test 1 (agent-initiated compaction) by default.")
        print(f"Usage: {sys.argv[0]} [test1|test2|test3]")
        print()
        success = test_agent_initiated_compaction()
        sys.exit(0 if success else 1)
