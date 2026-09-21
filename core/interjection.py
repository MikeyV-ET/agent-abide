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


def _parse_msg_ts(value) -> Optional[float]:
    """Inbox "ts" -> epoch seconds. Writers disagree on the spelling.

    The TUI writes an ISO-8601 string; adapter_api.write_to_adapter_inbox writes
    an epoch float. Anything else is treated as unknown rather than guessed at.
    """
    import datetime as _dt

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return _dt.datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def format_message_for_interjection(msg: dict, *, stage: str = "delivered",
                                    at: Optional[float] = None) -> str:
    """Format an adapter message dict for interjection delivery.

    Carries both ends of the trip, to the second:
      [sender (via adapter) (id=bell_xxx, sent Mon Sep 21 06:52:10 PDT, delivered 06:52:14)] text
      [localmail (id=bell_xxx, sent ..., delivered ...) from sender] text

    stage says what `at` means. "delivered" is only true for the stdin path,
    which writes into the agent's input on the spot. The BASH_ENV fallback is
    "queued": it lands whenever the next bash -c runs, which nobody here knows.
    """
    import time as _time

    text = msg.get("text", "").strip()
    sender = msg.get("from", "unknown")
    adapter = msg.get("adapter", "unknown")
    bell_id = msg.get("id", f"bell_{secrets.token_hex(4)}")

    when = at if at is not None else _time.time()
    sent = _parse_msg_ts(msg.get("ts"))
    if sent is not None:
        # Date and zone once, on the first stamp; the second is always same-day
        # enough to read as a clock time.
        stamps = (
            f"sent {_time.strftime('%a %b %d %H:%M:%S %Z', _time.localtime(sent))}, "
            f"{stage} {_time.strftime('%H:%M:%S', _time.localtime(when))}"
        )
    else:
        stamps = f"{stage} {_time.strftime('%a %b %d %H:%M:%S %Z', _time.localtime(when))}"

    if adapter == "localmail":
        return f"[localmail (id={bell_id}, {stamps}) from {sender}] {text}"
    return f"[{sender} (via {adapter}) (id={bell_id}, {stamps})] {text}"


async def interjection_watcher(agent_name: str, poll_fn, poll_interval: float = 2.0,
                               env=None, inject_fn=None):
    """Poll for incoming messages during BUSY turns.

    Primary (Claude): inject_fn(text) writes to binary stdin mid-turn
    (held until tool returns — Astro probe ddb0e7e).
    Fallback / Grok: queue_interjection → BASH_ENV hook on next bash -c.

    If inject_fn succeeds, skip disk queue to avoid double delivery.
    If inject_fn fails or is None, queue for BASH_ENV.

    Args:
        inject_fn: optional async callable(text) -> bool
    """
    import asyncio

    try:
        while True:
            await asyncio.sleep(poll_interval)
            try:
                msgs = poll_fn()
                for msg in msgs:
                    injected = False
                    if inject_fn is not None:
                        # Stamped immediately before the stdin write, which is
                        # the moment it lands in the agent's input.
                        text = format_message_for_interjection(msg, stage="delivered")
                        try:
                            result = inject_fn(text)
                            if asyncio.iscoroutine(result):
                                result = await result
                            injected = bool(result)
                        except Exception as e:
                            print(f"[interjection] inject_fn failed: {e}")
                    if not injected:
                        # BASH_ENV path: lands at the next bash -c, time unknown.
                        text = format_message_for_interjection(msg, stage="queued")
                        queue_interjection(agent_name, text, env=env)
                    # V1: log interjection when human text arrives mid-turn
                    try:
                        from asdaaas import write_conversation
                        try:
                            import asdaaas_runtime as _rt
                            sid = _rt.current_session_id
                        except Exception:
                            import asdaaas as _asdaaas_mod
                            sid = getattr(_asdaaas_mod, "_current_session_id", None)
                        write_conversation(
                            agent_name, "user", text, env=env,
                            session_id=sid, kind="interjection",
                            msg_id=msg.get("id"),
                        )
                    except Exception as e:
                        print(f"[asdaaas] write_conversation(interjection) failed: {e}")
                    print(f"[asdaaas] interjection queued for {agent_name}: {msg.get('from', '?')} via {msg.get('adapter', '?')}")
            except Exception as e:
                print(f"[asdaaas] interjection_watcher error (continuing): {e}")
    except asyncio.CancelledError:
        pass
