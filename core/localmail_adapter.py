#!/usr/bin/env python3
"""
Localmail Adapter — agent-side library for sending/reading localmail.

send_mail() writes to the CALLER's outbox. The localmail service daemon
picks it up and delivers to the target's inbox + rings doorbell.

This file is imported by agents. It never writes outside the caller's
own directory tree, so it works under sandbox and per-user Unix permissions.
"""

import json
import os
import sys
import time
import tempfile
import uuid
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from asdaaas_env import AsdaaasEnv


def send_mail(from_agent: str, to_agent, text: str,
              priority: int = 3, meta: dict = None,
              env: Optional[AsdaaasEnv] = None) -> str:
    """Send a localmail message by writing to the caller's outbox.

    to_agent: str or list[str]. The service daemon routes to recipients.
    Returns the message ID.
    """
    env = env or AsdaaasEnv.from_config()
    recipients = [to_agent] if isinstance(to_agent, str) else list(to_agent)
    if not recipients:
        raise ValueError("to_agent must be a non-empty string or list")

    msg_id = str(uuid.uuid4())

    outbox = env.adapter_dir(from_agent, "localmail") / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)

    msg = {
        "id": msg_id,
        "from": from_agent,
        "to": recipients if len(recipients) > 1 else recipients[0],
        "text": text,
        "priority": priority,
        "meta": meta or {},
        "ts": time.time(),
    }

    ts_prefix = f"mail_{int(time.time()*1000000):016d}_"
    fd, tmp_path = tempfile.mkstemp(dir=str(outbox), suffix=".tmp", prefix=ts_prefix)
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
    """Reply to all recipients + sender of an original message, excluding self."""
    original_to = original_msg.get("to", [])
    if isinstance(original_to, str):
        original_to = [original_to]
    original_from = original_msg.get("from", "")

    all_parties = set(original_to) | {original_from}
    all_parties.discard(from_agent)

    if not all_parties:
        raise ValueError("reply_all: no recipients after excluding self")

    return send_mail(from_agent, sorted(all_parties), text, priority, meta, env=env)


def read_mail(agent_name: str, delete: bool = True,
              env: Optional[AsdaaasEnv] = None) -> list:
    """Read all pending localmail for an agent (from inbox).

    For TUI agents who can't receive doorbells — call this to check mail.
    Returns list of message dicts, oldest first.
    """
    env = env or AsdaaasEnv.from_config()
    inbox = env.adapter_inbox(agent_name, "localmail")
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
    """Check mail without deleting."""
    return read_mail(agent_name, delete=False, env=env)
