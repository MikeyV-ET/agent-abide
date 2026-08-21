#!/usr/bin/env python3
"""
Session Adapter (Phase 6.2) — Compact and status commands for agents.
=====================================================================
Control adapter type. Accepts commands from agents (via inbox) and executes
them through the asdaaas command file interface.

Supported commands:
  compact  — Trigger manual compaction. Writes {"action": "compact"} to
             the asdaaas command file, waits for result, delivers doorbell.
  status   — Read agent health file and deliver current stats as doorbell.

This lets agents proactively compact themselves instead of waiting for
auto-compaction (which caused the stdio desync bug).

Inbox format (agent writes here):
  ~/asdaaas/adapters/session/inbox/<agent>/<msg>.json
  {
    "command": "compact",
    "from": "Trip",
    "request_id": "uuid"
  }

Doorbell format (delivered back to agent):
  {
    "adapter": "session",
    "command": "compact",
    "priority": 2,
    "text": "Compaction complete: 180000 -> 45000 tokens (75% reduction)",
    "request_id": "uuid",
    "result": {...}
  }

Usage:
    python3 session_adapter.py                          # watch all agents
    python3 session_adapter.py --agents Trip,Q,Cinco    # watch specific agents
"""

import json
import os
import sys
import time
import tempfile
import argparse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
import adapter_api

# ============================================================================
# PATHS
# ============================================================================

try:
    from asdaaas_config import config
except ModuleNotFoundError:
    import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'core'))
    from asdaaas_config import config
HUB_DIR = config.hub_dir
AGENTS_DIR = HUB_DIR / "agents"  # legacy
AGENTS_HOME_DIR = config.agents_home
SESSION_INBOX = HUB_DIR / "adapters" / "session" / "inbox"
# Legacy aliases for test monkeypatching
HEALTH_DIR = AGENTS_DIR
DOORBELL_DIR = AGENTS_DIR
COMMAND_DIR = AGENTS_DIR

ALL_AGENTS = ["Sr", "Jr", "Trip", "Q", "Cinco"]

# ============================================================================
# COMMAND HANDLERS
# ============================================================================

def handle_compact(agent, request_id):
    """Send a compact command through the asdaaas command file.
    
    Writes the command, then polls for the result file.
    Returns result dict or error dict.
    """
    agent_d = config.resolve_asdaaas_dir(agent, AGENTS_HOME_DIR)
    agent_d.mkdir(parents=True, exist_ok=True)
    
    # Write command for asdaaas to pick up
    cmd = {
        "action": "compact",
        "request_id": request_id,
        "ts": time.time(),
    }
    
    cmd_file = agent_d / "commands.json"
    tmp = str(cmd_file) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cmd, f)
    os.rename(tmp, str(cmd_file))
    
    print(f"[session] Sent compact command for {agent} (req={request_id})")
    
    # Wait for result file from asdaaas
    result_file = agent_d / "command_result.json"
    deadline = time.time() + 300  # 5 minute timeout for compaction
    
    while time.time() < deadline:
        if result_file.exists():
            try:
                with open(result_file) as f:
                    result = json.load(f)
                os.unlink(result_file)
                
                # Verify it's the right request
                if result.get("request_id") == request_id:
                    return result
                else:
                    # Wrong request — put it back and keep waiting
                    with open(str(result_file), "w") as f:
                        json.dump(result, f)
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(1.0)
    
    return {"error": "timeout", "detail": "Compact did not complete within 300s"}


def handle_status(agent):
    """Read agent health file and return current stats."""
    health_file = config.resolve_asdaaas_dir(agent, AGENTS_HOME_DIR) / "health.json"
    if not health_file.exists():
        return {"error": "no_health_file", "detail": f"No health file for {agent}"}
    
    try:
        with open(health_file) as f:
            health = json.load(f)
        
        total = health.get("totalTokens", 0)
        window = health.get("contextWindow", 200000)
        pct = (total / window * 100) if window > 0 else 0
        
        return {
            "agent": agent,
            "status": health.get("status", "unknown"),
            "totalTokens": total,
            "contextWindow": window,
            "usage_pct": round(pct, 1),
            "last_activity": health.get("last_activity", "unknown"),
            "detail": health.get("detail", ""),
        }
    except (json.JSONDecodeError, OSError) as e:
        return {"error": "read_error", "detail": str(e)}


# ============================================================================
# DOORBELL DELIVERY
# ============================================================================

def ring_session_doorbell(agent, command, request_id, result):
    """Write a session command result doorbell for an agent."""
    bell_dir = (config.resolve_asdaaas_dir(agent, AGENTS_HOME_DIR) / "doorbells")
    bell_dir.mkdir(parents=True, exist_ok=True)
    
    # Format human-readable text based on command type
    if command == "compact":
        if "error" in result:
            text = f"Compaction FAILED: {result.get('detail', 'unknown error')}"
            priority = 1
        else:
            before = result.get("before", 0)
            after = result.get("after", 0)
            if before > 0:
                reduction = round((1 - after / before) * 100)
                text = f"Compaction complete: {before} -> {after} tokens ({reduction}% reduction)"
            else:
                text = f"Compaction complete: {after} tokens"
            priority = 3
    elif command == "status":
        if "error" in result:
            text = f"Status query failed: {result.get('detail', 'unknown error')}"
            priority = 3
        else:
            pct = result.get("usage_pct", 0)
            total = result.get("totalTokens", 0)
            window = result.get("contextWindow", 0)
            status = result.get("status", "unknown")
            text = f"Status: {status}, context {pct}% ({total}/{window} tokens)"
            priority = 5
    else:
        text = f"Unknown command '{command}' result: {json.dumps(result)}"
        priority = 3
    
    bell = {
        "adapter": "session",
        "command": command,
        "priority": priority,
        "text": text,
        "request_id": request_id,
        "result": result,
        "ts": time.time(),
    }
    
    fd, tmp_path = tempfile.mkstemp(dir=str(bell_dir), suffix=".tmp", prefix="sess_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(bell, f)
        final = tmp_path.replace(".tmp", ".json")
        os.rename(tmp_path, final)
        print(f"[session] DOORBELL: {agent} {command} -> {text[:80]}")
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ============================================================================
# INBOX POLLING
# ============================================================================

def poll_session_inbox(agent):
    """Poll the session adapter's inbox for commands from an agent."""
    inbox = config.resolve_asdaaas_dir(agent, AGENTS_HOME_DIR) / "adapters" / "session" / "inbox"
    if not inbox.exists():
        return []
    
    commands = []
    for entry in sorted(inbox.iterdir()):
        if not entry.name.endswith(".json"):
            continue
        try:
            with open(entry, "r") as f:
                cmd = json.load(f)
            commands.append(cmd)
            entry.unlink()
        except (json.JSONDecodeError, OSError) as e:
            print(f"[session] inbox read error: {e}")
    
    return commands


# ============================================================================
# WATCH LOOP
# ============================================================================

def watch_loop(agents, poll_interval=2.0):
    """Main loop: watch inbox for commands, execute, deliver results."""
    print(f"[session] Session adapter starting")
    print(f"[session] Watching agents: {', '.join(agents)}")
    
    # Ensure inbox directories exist
    for agent in agents:
        (SESSION_INBOX / agent).mkdir(parents=True, exist_ok=True)
    
    # Register adapter
    adapter_api.register_adapter(
        name="session",
        capabilities=["control", "compact", "status"],
        config={
            "type": "control",
            "agents": agents,
            "commands": ["compact", "status"],
        },
    )
    
    heartbeat_interval = 30
    last_heartbeat = time.time()
    
    while True:
        try:
            for agent in agents:
                commands = poll_session_inbox(agent)
                
                for cmd in commands:
                    command = cmd.get("command", "")
                    request_id = cmd.get("request_id", cmd.get("id", f"auto_{int(time.time())}"))
                    
                    print(f"[session] Command from {agent}: {command} (req={request_id})")
                    
                    if command == "compact":
                        result = handle_compact(agent, request_id)
                        ring_session_doorbell(agent, "compact", request_id, result)
                    
                    elif command == "status":
                        result = handle_status(agent)
                        ring_session_doorbell(agent, "status", request_id, result)
                    
                    else:
                        print(f"[session] Unknown command: {command}")
                        ring_session_doorbell(agent, command, request_id, {
                            "error": "unknown_command",
                            "detail": f"Unknown command: {command}. Supported: compact, status",
                        })
            
            # Heartbeat
            now = time.time()
            if now - last_heartbeat >= heartbeat_interval:
                adapter_api.update_heartbeat("session")
                last_heartbeat = now
        
        except Exception as e:
            print(f"[session] Error: {e}")
            import traceback
            traceback.print_exc()
        
        time.sleep(poll_interval)


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="MikeyV Session Adapter (Phase 6.2)")
    parser.add_argument("--agents", default=None, help="Comma-separated agent list (default: all)")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Poll interval in seconds")
    args = parser.parse_args()
    
    if args.agents:
        agents = [a.strip() for a in args.agents.split(",")]
    else:
        agents = list(ALL_AGENTS)
    
    try:
        watch_loop(agents, args.poll_interval)
    except KeyboardInterrupt:
        print("\n[session] Shutting down.")
        adapter_api.deregister_adapter("session")


if __name__ == "__main__":
    main()
