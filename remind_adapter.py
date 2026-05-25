#!/usr/bin/env python3
"""
Remind Adapter — Self-nudge control adapter for agent turn planning.
====================================================================
Control adapter type. Agents write a command, hear a doorbell back
after a delay. Enables agents to plan their next turn before the
current one ends.

The agent writes:
    {"command": "remind", "delay": 0, "text": "Redirect gaze to pm:eric"}

After `delay` seconds, the agent hears:
    [remind] Redirect gaze to pm:eric

Use cases:
  - delay=0:   Immediate self-nudge. "Wake me up on the next turn."
  - delay=0.5: Near-immediate, after a brief pause.
  - delay=300: "Remind me in 5 minutes to check Trip's response."

This replaces raw doorbell file manipulation with a clean command interface.
The agent pushes a button. The adapter delivers the receipt.

Usage:
    python3 remind_adapter.py
    python3 remind_adapter.py --agents Sr,Jr,Trip,Q,Cinco
"""

import json
import os
import sys
import time
import tempfile
import argparse
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_api

def tprint(msg):
    """Timestamped print."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ============================================================================
# CONFIG
# ============================================================================

try:
    from asdaaas_config import config
except ModuleNotFoundError:
    import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'core'))
    from asdaaas_config import config
HUB_DIR = config.hub_dir
AGENTS_DIR = HUB_DIR / "agents"  # legacy
AGENTS_HOME_DIR = config.agents_home
DOORBELL_DIR = AGENTS_DIR  # legacy alias for test monkeypatching
ADAPTER_NAME = "remind"
POLL_INTERVAL = 0.25  # check for new commands 4x/sec

ALL_AGENTS = ["Sr", "Jr", "Trip", "Q", "Cinco"]


# ============================================================================
# DOORBELL DELIVERY
# ============================================================================

def deliver_doorbell(agent, text, priority=1):
    """Write a doorbell file for an agent."""
    bell_dir = AGENTS_HOME_DIR / agent / "asdaaas" / "doorbells"
    bell_dir.mkdir(parents=True, exist_ok=True)

    bell = {
        "adapter": ADAPTER_NAME,
        "priority": priority,
        "text": text,
        "ts": time.time(),
    }

    fd, tmp_path = tempfile.mkstemp(dir=str(bell_dir), suffix=".tmp", prefix="rem_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(bell, f)
        final = tmp_path.replace(".tmp", ".json")
        os.rename(tmp_path, final)
        tprint(f"[remind] DELIVERED: {agent} <- {text[:80]}")
    except Exception as e:
        tprint(f"[remind] ERROR writing doorbell for {agent}: {e}")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ============================================================================
# TIMER MANAGEMENT
# ============================================================================

class TimerPool:
    """Manage delayed doorbell deliveries using threads."""

    def __init__(self):
        self.active = []
        self._lock = threading.Lock()

    def schedule(self, agent, text, delay, priority=1):
        """Schedule a doorbell delivery after `delay` seconds."""
        if delay <= 0:
            # Immediate delivery
            deliver_doorbell(agent, text, priority)
            return

        def _fire():
            time.sleep(delay)
            deliver_doorbell(agent, text, priority)
            with self._lock:
                if t in self.active:
                    self.active.remove(t)

        t = threading.Thread(target=_fire, daemon=True)
        with self._lock:
            self.active.append(t)
        t.start()
        tprint(f"[remind] SCHEDULED: {agent} in {delay}s <- {text[:80]}")

    @property
    def count(self):
        with self._lock:
            return len(self.active)


# ============================================================================
# COMMAND PROCESSING
# ============================================================================

def process_command(cmd, agent, timers):
    """Process a remind command from an agent.

    Supported commands:
        {"command": "remind", "delay": 0, "text": "..."}
        {"command": "remind", "delay": 300.0, "text": "...", "priority": 2}
    """
    command = cmd.get("command", "")

    if command != "remind":
        tprint(f"[remind] UNKNOWN command from {agent}: {command}")
        deliver_doorbell(agent, f"error: unknown command '{command}'. Use: remind", priority=3)
        return

    text = cmd.get("text", "")
    if not text:
        deliver_doorbell(agent, "error: 'text' field required", priority=3)
        return

    delay = cmd.get("delay", 0)
    try:
        delay = float(delay)
    except (TypeError, ValueError):
        deliver_doorbell(agent, f"error: invalid delay '{delay}', must be a number", priority=3)
        return

    if delay < 0:
        delay = 0

    priority = cmd.get("priority", 1)

    timers.schedule(agent, text, delay, priority)


# ============================================================================
# MAIN LOOP
# ============================================================================

def run_adapter(agents):
    """Main loop: poll command inboxes, process remind commands."""
    tprint(f"[remind] Remind adapter starting")
    tprint(f"[remind] Watching agents: {', '.join(agents)}")

    adapter_api.register_adapter(
        name=ADAPTER_NAME,
        capabilities=["remind"],
        config={
            "type": "control",
            "agents": agents,
            "commands": ["remind"],
        },
    )

    timers = TimerPool()

    # Ensure per-agent inbox directories exist
    for agent in agents:
        inbox = AGENTS_HOME_DIR / agent / "asdaaas" / "adapters" / ADAPTER_NAME / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)

    last_heartbeat = time.time()

    while True:
        try:
            for agent in agents:
                # Poll this agent's remind inbox
                messages = adapter_api.poll_adapter_inbox(ADAPTER_NAME, agent)
                for msg in messages:
                    # The message text might be the command JSON, or the
                    # command might be in a structured field
                    cmd = None

                    # Try parsing text as JSON command
                    text = msg.get("text", "")
                    if text:
                        try:
                            cmd = json.loads(text)
                        except (json.JSONDecodeError, TypeError):
                            pass

                    # Fall back to meta field
                    if cmd is None:
                        cmd = msg.get("meta", {})

                    # Fall back to the message itself (if it has command key)
                    if not cmd.get("command") and msg.get("command"):
                        cmd = msg

                    if cmd and cmd.get("command"):
                        process_command(cmd, agent, timers)
                    else:
                        tprint(f"[remind] MALFORMED from {agent}: {str(msg)[:100]}")
                        deliver_doorbell(agent,
                            "error: malformed command. Expected: "
                            '{\"command\": \"remind\", \"delay\": 0, \"text\": \"...\"}',
                            priority=3)

            # Heartbeat
            now = time.time()
            if now - last_heartbeat >= 30:
                adapter_api.update_heartbeat(ADAPTER_NAME)
                last_heartbeat = now

        except Exception as e:
            tprint(f"[remind] ERROR: {e}")
            import traceback
            traceback.print_exc()

        time.sleep(POLL_INTERVAL)


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="MikeyV Remind Adapter")
    parser.add_argument("--agents", default=None, help="Comma-separated agent list (default: all)")
    args = parser.parse_args()

    if args.agents:
        agents = [a.strip() for a in args.agents.split(",")]
    else:
        agents = list(ALL_AGENTS)

    try:
        run_adapter(agents)
    except KeyboardInterrupt:
        print("\n[remind] Shutting down.")
        adapter_api.deregister_adapter(ADAPTER_NAME)


if __name__ == "__main__":
    main()
