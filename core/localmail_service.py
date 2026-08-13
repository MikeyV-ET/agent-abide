#!/usr/bin/env python3
"""
Localmail Service — daemon that routes messages between agents.

Watches all agents' outboxes for new messages, delivers to target inboxes,
and rings doorbells. Runs as a standalone process (not sandboxed).

This replaces the routing logic that was previously inside send_mail().
The adapter (localmail_adapter.py) writes to the sender's outbox;
this service picks up and delivers.

Also watches inboxes for direct-write messages (backward compat with
unsandboxed agents that still use the old send_mail path).

Usage:
  python3 localmail_service.py                  # watch all agents
  python3 localmail_service.py --agents Sr,Jr   # watch specific agents
"""

import json
import os
import sys
import time
import tempfile
import argparse
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_api

try:
    from asdaaas_config import config
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'core'))
    from asdaaas_config import config

from asdaaas_env import AsdaaasEnv

ALL_AGENTS = ["Sr", "Jr", "Trip", "Q", "Cinco", "Squiggy"]
PAYLOAD_MAX_AGE_SECONDS = 3600

_delivered_msg_ids: set = set()


# ============================================================================
# DELIVERY
# ============================================================================

def deliver_to_inbox(msg: dict, recipient: str, env: AsdaaasEnv):
    """Write a message to a recipient's inbox."""
    inbox = env.adapter_inbox(recipient, "localmail")
    inbox.mkdir(parents=True, exist_ok=True)

    ts_prefix = f"mail_{int(time.time()*1000000):016d}_"
    fd, tmp_path = tempfile.mkstemp(dir=str(inbox), suffix=".tmp", prefix=ts_prefix)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(msg, f)
        final = tmp_path.replace(".tmp", ".json")
        os.rename(tmp_path, final)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def ring_doorbell(agent_name: str, msg: dict, env: AsdaaasEnv):
    """Write a doorbell notification for an asdaaas-managed agent."""
    bell_dir = env.doorbells_dir(agent_name)
    bell_dir.mkdir(parents=True, exist_ok=True)

    msg_id = msg.get("id", "")
    if msg_id:
        dedup_key = f"{agent_name}:{msg_id}"
        if dedup_key in _delivered_msg_ids:
            return
        for existing in bell_dir.glob("bell_*.json"):
            try:
                with open(existing) as f:
                    if json.load(f).get("msg_id") == msg_id:
                        return
            except (json.JSONDecodeError, OSError):
                pass

    sender = msg.get("from", "unknown")
    text = msg.get("text", "")
    priority = msg.get("priority", 3)

    if len(text) > 500:
        payload_dir = env.adapter_dir(agent_name, "localmail") / "payloads"
        payload_dir.mkdir(parents=True, exist_ok=True)
        payload_path = payload_dir / f"{msg_id}.json"
        try:
            fd, tmp = tempfile.mkstemp(dir=str(payload_dir), suffix=".tmp", prefix="pay_")
            with os.fdopen(fd, "w") as f:
                json.dump(msg, f, indent=2)
            os.rename(tmp, str(payload_path))
        except Exception:
            try:
                os.unlink(tmp)
            except (OSError, UnboundLocalError):
                pass
        preview = text[:500] + "..."
        size_kb = len(text) / 1024
        approx_tokens = len(text) // 4
        bell_text = f"[localmail] Mail from {sender}:\n{preview}\n(Full message: cat {payload_path} — {size_kb:.1f}KB, ~{approx_tokens} tokens)"
    else:
        bell_text = f"[localmail] Mail from {sender}:\n{text}"

    bell = {
        "adapter": "localmail",
        "priority": priority,
        "text": bell_text,
        "from": sender,
        "msg_id": msg_id,
        "ts": time.time(),
    }

    fd, tmp_path = tempfile.mkstemp(dir=str(bell_dir), suffix=".tmp", prefix="bell_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(bell, f)
        final = tmp_path.replace(".tmp", ".json")
        os.rename(tmp_path, final)
        if msg_id:
            _delivered_msg_ids.add(f"{agent_name}:{msg_id}")
        print(f"[localmail] Doorbell: {sender} -> {agent_name} ({len(text)} chars, priority {priority})")
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_asdaaas_agents(agents: list, env: AsdaaasEnv) -> set:
    """Return the set of agents managed by asdaaas (can receive doorbells).

    Uses the agent list passed to the service rather than probing for
    doorbells dirs, which may not exist yet if no doorbell has been
    delivered since the last restart.
    """
    return set(agents)


# ============================================================================
# OUTBOX PROCESSING (new: adapter writes here)
# ============================================================================

def process_outboxes(agents: list, env: AsdaaasEnv):
    """Scan all agents' outboxes, deliver messages to recipients."""
    asdaaas_agents = get_asdaaas_agents(agents, env)

    for agent in agents:
        outbox = env.adapter_dir(agent, "localmail") / "outbox"
        if not outbox.exists():
            continue

        for entry in sorted(outbox.iterdir()):
            if not entry.name.endswith(".json"):
                continue

            try:
                with open(entry, "r") as f:
                    msg = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            recipients = msg.get("to", [])
            if isinstance(recipients, str):
                recipients = [recipients]

            sender = msg.get("from", agent)

            for recipient in recipients:
                if recipient in asdaaas_agents:
                    ring_doorbell(recipient, msg, env)
                else:
                    deliver_to_inbox(msg, recipient, env)
                    print(f"[localmail] {sender} -> {recipient} (inbox, TUI agent)")

            # Remove from outbox after delivery
            try:
                entry.unlink()
            except OSError:
                pass


# ============================================================================
# INBOX PROCESSING (backward compat: old send_mail wrote directly here)
# ============================================================================

def process_inboxes(agents: list, env: AsdaaasEnv):
    """Scan inboxes and ring doorbells for asdaaas agents.

    For TUI agents, messages stay in inbox — they poll with read_mail().
    For asdaaas agents, we ring a doorbell AND delete the inbox file.
    """
    asdaaas_agents = get_asdaaas_agents(agents, env)

    for agent in agents:
        inbox = env.adapter_inbox(agent, "localmail")
        if not inbox.exists():
            continue

        for entry in sorted(inbox.iterdir()):
            if not entry.name.endswith(".json"):
                continue

            try:
                with open(entry, "r") as f:
                    msg = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue

            if agent in asdaaas_agents:
                ring_doorbell(agent, msg, env)
                try:
                    entry.unlink()
                except OSError:
                    pass


# ============================================================================
# CLEANUP
# ============================================================================

def cleanup_old_payloads(agents: list, env: AsdaaasEnv):
    """Remove payload files older than PAYLOAD_MAX_AGE_SECONDS."""
    cutoff = time.time() - PAYLOAD_MAX_AGE_SECONDS
    removed = 0
    for agent in agents:
        payload_dir = env.adapter_dir(agent, "localmail") / "payloads"
        if not payload_dir.exists():
            continue
        for entry in payload_dir.iterdir():
            if not entry.name.endswith(".json"):
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                pass
    if removed:
        print(f"[localmail] Cleaned up {removed} stale payload file(s)")


# ============================================================================
# MAIN LOOP
# ============================================================================

def watch_loop(agents: list, poll_interval: float = 1.0,
               env: Optional[AsdaaasEnv] = None):
    """Main loop: watch outboxes and inboxes, deliver and ring doorbells."""
    env = env or AsdaaasEnv.from_config()
    print(f"[localmail-service] Starting localmail service")
    print(f"[localmail-service] Watching agents: {', '.join(agents)}")

    # Ensure directories exist
    for agent in agents:
        (env.adapter_inbox(agent, "localmail")).mkdir(parents=True, exist_ok=True)
        (env.adapter_dir(agent, "localmail") / "outbox").mkdir(parents=True, exist_ok=True)
        env.doorbells_dir(agent).mkdir(parents=True, exist_ok=True)

    adapter_api.register_adapter(
        name="localmail",
        capabilities=["send", "receive", "notify"],
        config={"type": "notify", "agents": agents},
    )

    heartbeat_interval = 30
    last_heartbeat = time.time()
    last_payload_cleanup = time.time()

    while True:
        try:
            process_outboxes(agents, env)
            process_inboxes(agents, env)

            now = time.time()
            if now - last_heartbeat >= heartbeat_interval:
                adapter_api.update_heartbeat("localmail")
                last_heartbeat = now
            if now - last_payload_cleanup >= PAYLOAD_MAX_AGE_SECONDS:
                cleanup_old_payloads(agents, env)
                last_payload_cleanup = now

        except Exception as e:
            print(f"[localmail-service] Error: {e}")
            import traceback
            traceback.print_exc()

        time.sleep(poll_interval)


def main():
    parser = argparse.ArgumentParser(description="MikeyV Localmail Service")
    parser.add_argument("--agents", default=None, help="Comma-separated agent list")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args()

    if args.agents:
        agents = [a.strip() for a in args.agents.split(",")]
    else:
        agents = list(ALL_AGENTS)

    try:
        watch_loop(agents, args.poll_interval)
    except KeyboardInterrupt:
        print("\n[localmail-service] Shutting down.")
        adapter_api.deregister_adapter("localmail")


if __name__ == "__main__":
    main()
