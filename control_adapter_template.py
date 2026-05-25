#!/usr/bin/env python3
"""
Control Adapter Template — Hub-connected adapter for controlling external tools.

Copy this file and customize for your specific tool (Impress, Meet, etc.).
The adapter registers with the hub, receives commands via its outbox,
executes them, and writes results back to the hub inbox.

Architecture:
  Hub inbox  <-- adapter writes results here
  Hub outbox --> adapter reads commands from here (hub routes commands to us)

Flow:
  1. Jr (or any agent) writes to hub inbox:
     adapter_api.write_message(to="impress", text='{"action":"advance_slide"}', adapter="orchestration")
  2. Hub sees to="impress" (a registered adapter), writes to impress outbox
  3. This adapter polls its outbox, picks up the command
  4. This adapter executes the command (YOUR CODE HERE)
  5. This adapter writes result to hub inbox (routed back to requesting agent)

Usage:
  # Copy and customize:
  cp control_adapter_template.py impress_adapter.py
  # Edit: change ADAPTER_NAME, add your control class, fill in execute_command()
  # Run:
  python3 impress_adapter.py

Author: MikeyV-Sr
"""

import json
import os
import sys
import time
import signal
import argparse
from pathlib import Path

# Add comms dir to path for adapter_api
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_api

# ============================================================================
# CUSTOMIZE THESE FOR YOUR ADAPTER
# ============================================================================

ADAPTER_NAME = "template"  # Change to: "impress", "meet", etc.
ADAPTER_CAPABILITIES = ["command", "status", "execute"]
ADAPTER_CONFIG = {
    "description": "Template control adapter — replace with your tool",
    # Add your config here: display, paths, etc.
}

# ============================================================================
# COMMAND HANDLER — YOUR CONTROL LOGIC GOES HERE
# ============================================================================

def execute_command(action: str, params: dict) -> dict:
    """
    Execute a command and return the result.

    THIS IS THE FUNCTION YOU CUSTOMIZE.

    For Impress, you'd import ImpressController and call its methods.
    For Meet, you'd import MeetController and call its methods.

    Args:
        action: The command to execute (e.g., "advance_slide", "join_call")
        params: Parameters for the command (e.g., {"count": 2})

    Returns:
        Dict with at least {"status": "ok"} or {"status": "error", "error": "..."}.
        Add any result data you want the requesting agent to see.
    """
    # --- EXAMPLE: Replace this with your actual control logic ---

    if action == "status":
        return {
            "status": "ok",
            "adapter": ADAPTER_NAME,
            "uptime_s": int(time.time() - _start_time),
            "commands_handled": _command_count,
        }

    if action == "ping":
        return {"status": "ok", "pong": True, "ts": time.time()}

    # --- IMPRESS EXAMPLE (uncomment and adapt): ---
    # from impress_control import ImpressController
    # ctrl = ImpressController()
    #
    # if action == "advance_slide":
    #     count = params.get("count", 1)
    #     ok = ctrl.advance_slide(count)
    #     info = ctrl.get_slide_info_atspi()
    #     return {"status": "ok" if ok else "error", "slide_info": info}
    #
    # if action == "start_presentation":
    #     ok = ctrl.start_presentation()
    #     return {"status": "ok" if ok else "error"}
    #
    # if action == "edit_text":
    #     text = params.get("text", "")
    #     target = params.get("target", "subtitle")
    #     ok = ctrl.edit_slide_text(text, target)
    #     return {"status": "ok" if ok else "error"}

    # --- MEET EXAMPLE (uncomment and adapt): ---
    # from meet_control import MeetController
    # ctrl = MeetController()
    #
    # if action == "join":
    #     code = params.get("meeting_code", "")
    #     ok = ctrl.join_meeting(code)
    #     return {"status": "ok" if ok else "error"}
    #
    # if action == "speak":
    #     text = params.get("text", "")
    #     # Use meet_speak.py logic
    #     return {"status": "ok"}

    return {"status": "error", "error": f"Unknown action: {action}"}


# ============================================================================
# ADAPTER CORE — Probably don't need to change anything below here
# ============================================================================

_start_time = time.time()
_command_count = 0
_running = True


def handle_command(msg: dict) -> dict:
    """Parse a command message and route to execute_command()."""
    global _command_count
    _command_count += 1

    text = msg.get("text", "")
    sender = msg.get("from", "unknown")
    msg_id = msg.get("request_id", msg.get("id", "unknown"))
    meta = msg.get("meta", {})
    origin_adapter = meta.get("origin_adapter", "unknown")

    # Parse command — expect JSON text with action + params
    try:
        cmd = json.loads(text)
        action = cmd.get("action", "")
        params = cmd.get("params", {})
    except (json.JSONDecodeError, AttributeError):
        # Plain text — treat entire text as action name
        action = text.strip()
        params = {}

    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] CMD from {sender}: action={action} params={params}")

    # Execute
    try:
        result = execute_command(action, params)
    except Exception as e:
        result = {"status": "error", "error": str(e)}
        print(f"[{ts}] ERROR: {e}")

    # Add metadata to result
    result["action"] = action
    result["adapter"] = ADAPTER_NAME
    result["ts"] = time.time()

    # Write result back to hub inbox, addressed to the origin adapter
    # so it gets routed back to the requesting agent
    adapter_api.write_message(
        to=sender,
        text=json.dumps(result),
        adapter=ADAPTER_NAME,
        sender=ADAPTER_NAME,
        meta={
            "type": "response",
            "request_id": msg_id,
            "origin_adapter": origin_adapter,
        },
    )

    print(f"[{ts}] RESULT: {result.get('status')} -> {sender}")
    return result


def run_adapter(poll_interval: float = 0.5, heartbeat_interval: float = 60.0):
    """Main loop: poll outbox for commands, execute, write results."""
    global _running

    # Register with hub
    adapter_api.register_adapter(
        name=ADAPTER_NAME,
        capabilities=ADAPTER_CAPABILITIES,
        config=ADAPTER_CONFIG,
    )
    print(f"[{ADAPTER_NAME}] Registered with hub")
    print(f"[{ADAPTER_NAME}] Polling outbox every {poll_interval}s")
    print(f"[{ADAPTER_NAME}] Heartbeat every {heartbeat_interval}s")

    last_heartbeat = time.time()

    while _running:
        # Poll for commands
        commands = adapter_api.poll_responses(ADAPTER_NAME)
        for cmd in commands:
            handle_command(cmd)

        # Periodic heartbeat
        now = time.time()
        if now - last_heartbeat >= heartbeat_interval:
            adapter_api.update_heartbeat(ADAPTER_NAME)
            last_heartbeat = now

        time.sleep(poll_interval)

    # Clean shutdown
    adapter_api.deregister_adapter(ADAPTER_NAME)
    print(f"[{ADAPTER_NAME}] Deregistered. Goodbye.")


def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully."""
    global _running
    print(f"\n[{ADAPTER_NAME}] Shutting down...")
    _running = False


def main():
    parser = argparse.ArgumentParser(
        description=f"MikeyV {ADAPTER_NAME} control adapter")
    parser.add_argument("--poll-interval", type=float, default=0.5,
                        help="Outbox poll interval in seconds (default: 0.5)")
    parser.add_argument("--heartbeat-interval", type=float, default=60.0,
                        help="Heartbeat interval in seconds (default: 60)")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print(f"{'=' * 50}")
    print(f"MikeyV Control Adapter: {ADAPTER_NAME}")
    print(f"{'=' * 50}")

    run_adapter(
        poll_interval=args.poll_interval,
        heartbeat_interval=args.heartbeat_interval,
    )


if __name__ == "__main__":
    main()
