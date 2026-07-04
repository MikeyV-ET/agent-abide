#!/usr/bin/env python3
"""
MikeyV Localmail Adapter — Async agent-to-agent messaging via filesystem.
=========================================================================
Notify adapter type. Agents write messages to each other's inboxes.
Localmail watches for new messages and rings doorbells via ASDAAAS.

For asdaaas agents: doorbell carries full message content (inline).
For TUI agents: message stays in inbox, agent polls with read_localmail().

Directory structure:
  ~/agents/<agent>/asdaaas/adapters/localmail/inbox/   — messages TO this agent

Doorbell format (written to ~/agents/<agent>/asdaaas/doorbells/):
  {
    "adapter": "localmail",
    "priority": 3,
    "text": "Mail from Jr: <message content>",
    "from": "Jr",
    "msg_id": "uuid"
  }

Usage:
  python3 localmail.py                  # watch all agents
  python3 localmail.py --agents Sr,Jr   # watch specific agents

Sending mail (from any agent or script):
  python3 -c "
  import sys; sys.path.insert(0, '/home/eric/projects/agent-abide/core')
  from localmail import send_mail
  send_mail(from_agent='Jr', to_agent='Q', text='Status update please')
  "

Reading mail (for TUI agents):
  python3 -c "
  import sys; sys.path.insert(0, '/home/eric/projects/agent-abide/core')
  from localmail import read_mail
  for msg in read_mail('Jr'): print(f'{msg[\"from\"]}: {msg[\"text\"]}')
  "
"""

import json
import os
import sys
import time
import tempfile
import argparse
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_api

# ============================================================================
# PATHS
# ============================================================================

from typing import Optional

try:
    from asdaaas_config import config
except ModuleNotFoundError:
    import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'core'))
    from asdaaas_config import config

from asdaaas_env import AsdaaasEnv

HUB_DIR = config.hub_dir
AGENTS_DIR = HUB_DIR / "agents"  # legacy
AGENTS_HOME_DIR = config.agents_home
LOCALMAIL_DIR = HUB_DIR / "adapters" / "localmail"
INBOX_DIR = LOCALMAIL_DIR / "inbox"
DOORBELL_DIR = AGENTS_DIR  # legacy alias for test monkeypatching

ALL_AGENTS = ["Sr", "Jr", "Trip", "Q", "Cinco", "Squiggy"]

# ============================================================================
# SEND / READ API (importable by agents)
# ============================================================================

def send_mail(from_agent: str, to_agent, text: str, 
              priority: int = 3, meta: dict = None,
              env: Optional[AsdaaasEnv] = None) -> str:
    """
    Send a localmail message to one or more agents.
    
    to_agent: str or list[str].  When a list is given the same message
    (same msg_id) is delivered to every recipient.  Each recipient's
    copy records the full recipient list in ``to`` so agents can
    reply-all.
    
    Can be called from any context — TUI agent, asdaaas agent, script.
    Returns the message ID.
    """
    env = env or AsdaaasEnv.from_config()
    recipients = [to_agent] if isinstance(to_agent, str) else list(to_agent)
    if not recipients:
        raise ValueError("to_agent must be a non-empty string or list")
    
    import uuid
    msg_id = str(uuid.uuid4())
    
    for recipient in recipients:
        inbox = env.agents_home / recipient / "asdaaas" / "adapters" / "localmail" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        
        msg = {
            "id": msg_id,
            "from": from_agent,
            "to": recipients if len(recipients) > 1 else recipient,
            "text": text,
            "priority": priority,
            "meta": meta or {},
            "ts": time.time(),
        }
        
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
    
    return msg_id


def reply_all(original_msg: dict, from_agent: str, text: str,
              priority: int = 3, meta: dict = None,
              env: Optional[AsdaaasEnv] = None) -> str:
    """Reply to all recipients + sender of an original message, excluding self.
    
    original_msg: the message dict being replied to (must have 'from' and 'to').
    from_agent: who is sending the reply.
    
    Recipient list = original sender + all original recipients, minus from_agent.
    Returns the message ID.
    """
    original_to = original_msg.get("to", [])
    if isinstance(original_to, str):
        original_to = [original_to]
    original_from = original_msg.get("from", "")
    
    all_parties = set(original_to) | {original_from}
    all_parties.discard(from_agent)  # don't send to self
    
    if not all_parties:
        raise ValueError("reply_all: no recipients after excluding self")
    
    return send_mail(from_agent, sorted(all_parties), text, priority, meta, env=env)


def read_mail(agent_name: str, delete: bool = True,
              env: Optional[AsdaaasEnv] = None) -> list:
    """
    Read all pending localmail for an agent.
    
    For TUI agents who can't receive doorbells — call this to check mail.
    Returns list of message dicts, oldest first.
    """
    env = env or AsdaaasEnv.from_config()
    inbox = env.agents_home / agent_name / "asdaaas" / "adapters" / "localmail" / "inbox"
    if not inbox.exists():
        return []
    
    messages = []
    for entry in sorted(inbox.iterdir()):
        if not entry.name.endswith(".json"):
            continue
        try:
            with open(entry, "r") as f:
                data = json.load(f)
            messages.append(data)
            if delete:
                entry.unlink()
        except json.JSONDecodeError:
            pass
        except Exception as e:
            print(f"[localmail] Error reading {entry}: {e}")
    
    return messages


def peek_mail(agent_name: str, env: Optional[AsdaaasEnv] = None) -> list:
    """Check mail without deleting. Returns list of message dicts."""
    return read_mail(agent_name, delete=False, env=env)


# ============================================================================
# DOORBELL WRITING
# ============================================================================

_delivered_msg_ids: set[str] = set()  # track delivered msg_ids across ack cycles

def ring_doorbell(agent_name: str, msg: dict,
                  env: Optional[AsdaaasEnv] = None):
    """Write a doorbell notification for an asdaaas-managed agent."""
    env = env or AsdaaasEnv.from_config()
    bell_dir = env.agents_home / agent_name / "asdaaas" / "doorbells"
    bell_dir.mkdir(parents=True, exist_ok=True)
    
    # Deduplicate: skip if already delivered (in-memory) or bell exists on disk.
    # The in-memory set catches re-rings after the agent acks (deletes) the bell
    # (issue_0040: disk-only dedup fails when bell is acked between rings).
    msg_id = msg.get("id", "")
    if msg_id:
        dedup_key = f"{agent_name}:{msg_id}"
        if dedup_key in _delivered_msg_ids:
            print(f"[localmail] Duplicate skipped (already delivered): {msg_id} for {agent_name}")
            return
        for existing in bell_dir.glob("bell_*.json"):
            try:
                with open(existing) as f:
                    if json.load(f).get("msg_id") == msg_id:
                        print(f"[localmail] Duplicate skipped: {msg_id} for {agent_name}")
                        return
            except (json.JSONDecodeError, OSError):
                pass
    
    sender = msg.get("from", "unknown")
    text = msg.get("text", "")
    priority = msg.get("priority", 3)
    msg_id = msg.get("id", "")
    
    # For long messages, write a payload file and reference it in the doorbell.
    # The agent can read the full message from the payload path.
    # (Bug fix: previously truncated to 500 chars and said "full message in inbox",
    # but the inbox file was deleted. Agent got truncated text with no recovery path.
    # Trip hit this 3x in Session 42.)
    if len(text) > 500:
        payload_dir = env.agents_home / agent_name / "asdaaas" / "adapters" / "localmail" / "payloads"
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


# ============================================================================
# ASDAAAS AGENT DETECTION
# ============================================================================

def get_asdaaas_agents(env: Optional[AsdaaasEnv] = None):
    """Detect which agents are running on asdaaas (can receive doorbells).
    
    An agent is considered asdaaas-capable if it has an asdaaas/doorbells/
    directory. This is always true for agents set up with setup_agent.sh or
    auto-generated by asdaaas.py on first run. Health file freshness is NOT
    checked -- idle agents on 'delay until_event' may have stale health files
    but must still receive doorbells (that's how they wake up).
    """
    env = env or AsdaaasEnv.from_config()
    if not env.agents_home.exists():
        return set()
    
    asdaaas_agents = set()
    
    for agent_d in env.agents_home.iterdir():
        if not agent_d.is_dir():
            continue
        # Agent has asdaaas infrastructure = can receive doorbells
        if (agent_d / "asdaaas" / "doorbells").is_dir():
            asdaaas_agents.add(agent_d.name)
    
    return asdaaas_agents


# ============================================================================
# WATCHER LOOP
# ============================================================================

PAYLOAD_MAX_AGE_SECONDS = 3600  # clean up payload files older than 1 hour


def _cleanup_old_payloads(agents: list, env: Optional[AsdaaasEnv] = None):
    """Remove payload files older than PAYLOAD_MAX_AGE_SECONDS.

    Payload files are created for long messages so the agent can cat the
    full text.  Once the doorbell has been delivered and acked (or expired),
    the payload is stale.  Without cleanup, payloads accumulate forever
    (issue_0002).
    """
    env = env or AsdaaasEnv.from_config()
    cutoff = time.time() - PAYLOAD_MAX_AGE_SECONDS
    removed = 0
    for agent in agents:
        payload_dir = env.agents_home / agent / "asdaaas" / "adapters" / "localmail" / "payloads"
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


def watch_loop(agents: list, poll_interval: float = 1.0,
               env: Optional[AsdaaasEnv] = None):
    """Main loop: watch inboxes, ring doorbells for asdaaas agents.
    
    For TUI agents, messages stays in inbox — they poll with read_mail().
    For asdaaas agents, we ring a doorbell AND delete the inbox file
    (the doorbell carries the content inline).
    """
    env = env or AsdaaasEnv.from_config()
    print(f"[localmail] Starting localmail adapter")
    print(f"[localmail] Watching agents: {', '.join(agents)}")
    
    # Ensure directories exist
    for agent in agents:
        (env.agents_home / agent / "asdaaas" / "adapters" / "localmail" / "inbox").mkdir(parents=True, exist_ok=True)
    
    # Register adapter
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
            # Detect which agents are on asdaaas
            asdaaas_agents = get_asdaaas_agents(env=env)
            
            for agent in agents:
                inbox = env.agents_home / agent / "asdaaas" / "adapters" / "localmail" / "inbox"
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
                    
                    sender = msg.get("from", "unknown")
                    text = msg.get("text", "")
                    
                    if agent in asdaaas_agents:
                        # Agent is on asdaaas — ring doorbell with inline content
                        ring_doorbell(agent, msg, env=env)
                        try:
                            entry.unlink()
                        except OSError:
                            pass
                    else:
                        # Agent is on TUI or unknown — leave message in inbox
                        # They'll poll with read_mail()
                        print(f"[localmail] {sender} -> {agent} (inbox, TUI agent)")
            
            # Heartbeat + periodic cleanup
            now = time.time()
            if now - last_heartbeat >= heartbeat_interval:
                adapter_api.update_heartbeat("localmail")
                last_heartbeat = now
            if now - last_payload_cleanup >= PAYLOAD_MAX_AGE_SECONDS:
                _cleanup_old_payloads(agents, env=env)
                last_payload_cleanup = now
            
        except Exception as e:
            print(f"[localmail] Error: {e}")
            import traceback
            traceback.print_exc()
        
        time.sleep(poll_interval)


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="MikeyV Localmail Adapter")
    parser.add_argument("--agents", default=None, help="Comma-separated agent list (default: all)")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="Poll interval in seconds")
    args = parser.parse_args()
    
    if args.agents:
        agents = [a.strip() for a in args.agents.split(",")]
    else:
        agents = list(ALL_AGENTS)
    
    try:
        watch_loop(agents, args.poll_interval)
    except KeyboardInterrupt:
        print("\n[localmail] Shutting down.")
        adapter_api.deregister_adapter("localmail")


if __name__ == "__main__":
    main()
