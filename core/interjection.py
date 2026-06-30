"""Mid-turn message interjection queue.

Queue messages for delivery to agents during tool calls
via the BASH_ENV hook (interjection_hook.sh).
"""

import os
import secrets
import time
from pathlib import Path


def interjection_dir(agent_name: str) -> Path:
    """Return the interjection queue directory for an agent."""
    return Path.home() / "agents" / agent_name / "asdaaas" / "interjections"


def queue_interjection(agent_name: str, text: str) -> None:
    """Queue a message for mid-turn delivery via BASH_ENV hook.

    Writes to ~/agents/{agent_name}/asdaaas/interjections/interject_{timestamp_ms}_{pid}.txt
    Uses atomic write: .tmp first, then rename to .txt so the hook
    never reads a partially-written file.
    """
    dest = interjection_dir(agent_name)
    dest.mkdir(parents=True, exist_ok=True)

    timestamp_ms = int(time.time() * 1000)
    rand = secrets.token_hex(4)
    target = dest / f"interject_{timestamp_ms}_{os.getpid()}_{rand}.txt"
    tmp = target.with_suffix(".tmp")

    tmp.write_text(text)
    tmp.rename(target)


def drain_interjection_queue(agent_name: str) -> list[str]:
    """Drain any unconsumed messages from the interjection queue.

    Called by asdaaas during post-response processing. Returns the text
    of each unconsumed message and removes the files. Messages left in
    the queue were queued after the last shell tool call — the hook
    never had a chance to deliver them.

    Returns an empty list if the queue is empty or doesn't exist.
    """
    d = interjection_dir(agent_name)
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
