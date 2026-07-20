#!/usr/bin/env python3
"""
MikeyV Sixel Email Adapter — Secure agent-to-human email via sixel.email.
=========================================================================
Notify adapter type. Agents send emails to their human via the sixel.email
API. The adapter polls for replies and rings doorbells.

Each agent has its own sixel.email account (separate API key). Credentials
are stored in the agent's home at .mikeyv_creds/sixel.json.

Sending (from agent code):
  python3 -c "
  import sys; sys.path.insert(0, '/srv/agent-abide/core')
  from sixel_adapter import send_email
  send_email('Sr', subject='Status update', body='All tests passing.')
  "

The adapter watches all configured agents, polling their inboxes and
delivering replies as doorbells.
"""

import json
import os
import sys
import time
import tempfile
import argparse
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_api

try:
    from asdaaas_config import config
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'core'))
    from asdaaas_config import config

# ============================================================================
# CREDENTIALS
# ============================================================================

def _load_creds(agent_name: str) -> dict:
    """Load sixel.email credentials for an agent."""
    # Check agent home first, then shared creds dir
    agent_home = config.agent_home(agent_name)
    for path in [
        agent_home / ".mikeyv_creds" / "sixel.json",
        Path.home() / ".mikeyv_creds" / f"sixel_{agent_name.lower()}.json",
    ]:
        if path.exists():
            with open(path) as f:
                return json.load(f)
    raise FileNotFoundError(f"No sixel.email credentials found for {agent_name}")


def _api_call(method: str, endpoint: str, token: str, data: dict = None) -> dict:
    """Make an API call to sixel.email."""
    url = f"https://sixel.email/v1{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        print(f"[sixel] API error {e.code}: {error_body}")
        raise
    except URLError as e:
        print(f"[sixel] Network error: {e.reason}")
        raise


# ============================================================================
# SEND API (importable by agents)
# ============================================================================

def send_email(agent_name: str, subject: str, body: str) -> dict:
    """Send an email from an agent to their human via sixel.email.
    
    Returns the API response dict with 'id', 'status', 'credits_remaining'.
    """
    creds = _load_creds(agent_name)
    result = _api_call("POST", "/send", creds["token"], {
        "subject": subject,
        "body": body,
    })
    print(f"[sixel] {agent_name} sent email: {subject} (credits: {result.get('credits_remaining', '?')})")
    return result


# ============================================================================
# DOORBELL WRITING
# ============================================================================

_delivered_ids: set = set()

def _ring_doorbell(agent_name: str, msg: dict):
    """Write a doorbell for an inbound email."""
    msg_id = msg.get("id", "")
    if msg_id in _delivered_ids:
        return
    
    bell_dir = config.agent_doorbells_dir(agent_name)
    bell_dir.mkdir(parents=True, exist_ok=True)
    
    # Check disk for existing bell with same msg_id
    for existing in bell_dir.glob("bell_*.json"):
        try:
            with open(existing) as f:
                if json.load(f).get("msg_id") == msg_id:
                    _delivered_ids.add(msg_id)
                    return
        except (json.JSONDecodeError, OSError):
            pass
    
    sender = msg.get("from", "unknown")
    subject = msg.get("subject", "(no subject)")
    body = msg.get("body", "")
    
    bell_text = f"[sixel.email] Mail from {sender}:\nSubject: {subject}\n\n{body}"
    
    bell = {
        "adapter": "sixel",
        "priority": 2,
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
        _delivered_ids.add(msg_id)
        print(f"[sixel] Doorbell: {sender} -> {agent_name} (subject: {subject})")
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ============================================================================
# WATCHER LOOP
# ============================================================================

def watch_loop(agents: list, poll_interval: float = 60.0):
    """Main loop: poll sixel.email inboxes, ring doorbells for replies."""
    print(f"[sixel] Starting sixel.email adapter")
    print(f"[sixel] Watching agents: {', '.join(agents)}")
    
    # Load credentials for all agents
    agent_creds = {}
    for agent in agents:
        try:
            agent_creds[agent] = _load_creds(agent)
            print(f"[sixel] Loaded credentials for {agent}")
        except FileNotFoundError as e:
            print(f"[sixel] Skipping {agent}: {e}")
    
    if not agent_creds:
        print("[sixel] No agents with credentials found. Exiting.")
        return
    
    adapter_api.register_adapter(
        name="sixel",
        capabilities=["send", "receive", "notify"],
        config={"type": "notify", "agents": list(agent_creds.keys())},
    )
    
    # Track last seen message ID per agent to avoid re-delivery
    last_seen: dict[str, str] = {}
    
    while True:
        for agent, creds in agent_creds.items():
            try:
                result = _api_call("GET", "/inbox", creds["token"])
                messages = result.get("messages", [])
                
                for msg in messages:
                    msg_id = msg.get("id", "")
                    if msg_id and msg_id == last_seen.get(agent):
                        continue
                    _ring_doorbell(agent, msg)
                
                if messages:
                    last_seen[agent] = messages[-1].get("id", "")
                    
            except Exception as e:
                print(f"[sixel] Error polling {agent}: {e}")
        
        time.sleep(poll_interval)


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="MikeyV Sixel Email Adapter")
    parser.add_argument("--agents", default=None, help="Comma-separated agent list")
    parser.add_argument("--poll-interval", type=float, default=60.0, help="Poll interval in seconds")
    args = parser.parse_args()
    
    if args.agents:
        agents = [a.strip() for a in args.agents.split(",")]
    else:
        agents = list(config.agents.keys())
    
    try:
        watch_loop(agents, args.poll_interval)
    except KeyboardInterrupt:
        print("\n[sixel] Shutting down.")
        adapter_api.deregister_adapter("sixel")


if __name__ == "__main__":
    main()
