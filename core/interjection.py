"""Mid-turn message interjection queue.

Queue messages for delivery to agents during tool calls
via the BASH_ENV hook (interjection_hook.sh).
"""

import os
import secrets
import time
from pathlib import Path
from typing import Optional


def interjection_dir(agent_name: str, env=None) -> Path:
    """Return the interjection queue directory for an agent.

    Uses agents.json home via env.agent_asdaaas_dir (nested homes OK).
    """
    if env is None:
        from asdaaas_env import AsdaaasEnv
        env = AsdaaasEnv.from_config()
    return env.agent_asdaaas_dir(agent_name) / "interjections"


def queue_interjection(agent_name: str, text: str, env=None) -> None:
    """Queue a message for mid-turn delivery via BASH_ENV hook.

    Writes to {agent_home}/asdaaas/interjections/interject_{timestamp_ms}_{pid}.txt
    Uses atomic write: .tmp first, then rename to .txt so the hook
    never reads a partially-written file.
    """
    dest = interjection_dir(agent_name, env=env)
    dest.mkdir(parents=True, exist_ok=True)

    timestamp_ms = int(time.time() * 1000)
    rand = secrets.token_hex(4)
    target = dest / f"interject_{timestamp_ms}_{os.getpid()}_{rand}.txt"
    tmp = target.with_suffix(".tmp")

    tmp.write_text(text)
    tmp.rename(target)


def drain_interjection_queue(agent_name: str, env=None) -> list[str]:
    """Drain any unconsumed messages from the interjection queue.

    Called by asdaaas during post-response processing. Returns the text
    of each unconsumed message and removes the files. Messages left in
    the queue were queued after the last shell tool call — the hook
    never had a chance to deliver them.

    Returns an empty list if the queue is empty or doesn't exist.
    """
    d = interjection_dir(agent_name, env=env)
    if not d.exists():
        return []

    messages = []
    for f in sorted(d.glob("*.txt")):
        try:
            messages.append(f.read_text())
            f.unlink()
        except (OSError, FileNotFoundError):
            pass
    return messages


def format_message_for_interjection(msg: dict) -> str:
    """Format an adapter message dict for interjection delivery.

    Produces the same format agents see in doorbells, with ID for acking:
      [sender (via adapter) (id=bell_xxx, ts=...) ] message text

    For localmail: [localmail (id=bell_xxx, ts=...) from sender] text
    """
    import time as _time

    text = msg.get("text", "").strip()
    sender = msg.get("from", "unknown")
    adapter = msg.get("adapter", "unknown")
    bell_id = msg.get("id", f"bell_{secrets.token_hex(4)}")
    ts = _time.strftime("%a %b %d %H:%M %Z")

    if adapter == "localmail":
        return f"[localmail (id={bell_id}, ts={ts}) from {sender}] {text}"
    else:
        return f"[{sender} (via {adapter}) (id={bell_id}, ts={ts})] {text}"


async def interjection_watcher(agent_name: str, poll_fn, poll_interval: float = 2.0,
                               env=None):
    """Poll for incoming messages during BUSY turns and route to interjection queue.

    Args:
        agent_name: Agent to queue interjections for.
        poll_fn: Callable that returns list of message dicts (destructive poll).
                 In asdaaas this is: lambda: poll_adapter_inboxes(agent_name, awareness)
        poll_interval: Seconds between polls.
        env: Optional AsdaaasEnv for path resolution.

    Runs as an asyncio.Task alongside collect_response(). Caller cancels
    when the turn completes.
    """
    import asyncio

    try:
        while True:
            await asyncio.sleep(poll_interval)
            try:
                msgs = poll_fn()
                for msg in msgs:
                    text = format_message_for_interjection(msg)
                    queue_interjection(agent_name, text, env=env)
                    print(f"[asdaaas] interjection queued for {agent_name}: {msg.get('from', '?')} via {msg.get('adapter', '?')}")
            except Exception as e:
                print(f"[asdaaas] interjection_watcher error (continuing): {e}")
    except asyncio.CancelledError:
        pass
