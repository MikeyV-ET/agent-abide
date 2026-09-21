"""Session / usage limit handling (backend-agnostic).

When a model binary hits a session or usage limit, asdaaas should:
  1. Know (health + conversation control + logs)
  2. Park inbound messages (doorbells/inbox keep accumulating; no continue thrash)
  3. Wake when the limit resets (delay until then, then restart + doorbell)

Backends detect and set stop_reason / flags; this module owns the policy.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Text patterns (Claude Max, API, generic)
LIMIT_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"session limit",
        r"usage limit",
        r"rate limit",
        r"hit your limit",
        r"you've hit",
        r"you have hit",
        r"limit reached",
        r"limit exceeded",
        r"quota exceeded",
        r"too many requests",
        r"try again (in|at|after)",
        r"resets?\s+(at|in|on)",
        r"out of (usage|quota|credits)",
    )
]

# "resets at 2:20am", "try again in 4 hours", "available again at 2026-09-21T09:00:00Z"
RESET_PATTERNS = [
    re.compile(r"resets?\s+at\s+(\d{1,2}):(\d{2})\s*(am|pm)?\s*(pacific|pt|utc|gmt)?", re.I),
    re.compile(r"try again in\s+(\d+)\s*(hour|hr|minute|min|second|sec)s?", re.I),
    re.compile(r"available again at\s+(\S+)", re.I),
    re.compile(r"retry after\s+(\d+)", re.I),  # seconds
]


@dataclass
class SessionLimitInfo:
    detected: bool = False
    source: str = ""
    raw_snippet: str = ""
    reset_unix: Optional[float] = None  # when to wake; None = unknown (manual / long delay)
    reason: str = "session_limit"


def text_indicates_limit(text: str) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in LIMIT_PATTERNS)


def parse_reset_unix(text: str, *, now: Optional[float] = None) -> Optional[float]:
    """Best-effort parse of reset time from limit message. None if unknown."""
    if not text:
        return None
    now = now if now is not None else time.time()

    m = RESET_PATTERNS[1].search(text)  # try again in N units
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        mult = 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
        return now + n * mult

    m = RESET_PATTERNS[3].search(text)  # retry after N seconds
    if m:
        return now + int(m.group(1))

    m = RESET_PATTERNS[0].search(text)  # resets at HH:MM am/pm
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2))
        ampm = (m.group(3) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        # Assume local wall clock
        lt = time.localtime(now)
        candidate = time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1)
        )
        if candidate <= now:
            candidate += 86400  # tomorrow
        return candidate

    m = RESET_PATTERNS[2].search(text)
    if m:
        raw = m.group(1).strip().rstrip(".")
        try:
            # ISO-ish
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            pass

    return None


def inspect_limit_text(text: str, *, source: str = "text") -> SessionLimitInfo:
    info = SessionLimitInfo()
    if not text_indicates_limit(text or ""):
        return info
    info.detected = True
    info.source = source
    info.raw_snippet = (text or "")[:400]
    info.reset_unix = parse_reset_unix(text)
    return info


def park_path(agent_dir: Path) -> Path:
    return Path(agent_dir) / "session_limit.json"


def write_park_state(agent_dir: Path, info: SessionLimitInfo) -> Path:
    path = park_path(agent_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "status": "session_limited",
        "reason": info.reason,
        "source": info.source,
        "snippet": info.raw_snippet,
        "reset_unix": info.reset_unix,
        "reset_iso": (
            datetime.fromtimestamp(info.reset_unix, tz=timezone.utc).isoformat()
            if info.reset_unix
            else None
        ),
        "parked_at": time.time(),
        "parked_at_iso": datetime.now(timezone.utc).isoformat(),
    }
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    import os
    os.rename(tmp, str(path))
    return path


def clear_park_state(agent_dir: Path) -> None:
    p = park_path(agent_dir)
    try:
        p.unlink(missing_ok=True)
    except Exception:
        pass


def read_park_state(agent_dir: Path) -> Optional[dict]:
    p = park_path(agent_dir)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def seconds_until_reset(info: SessionLimitInfo, *, now: Optional[float] = None) -> Optional[float]:
    now = now if now is not None else time.time()
    if info.reset_unix is None:
        return None
    return max(0.0, info.reset_unix - now)


def handle_session_limit(
    agent_name: str,
    info: SessionLimitInfo,
    *,
    env=None,
    total_tokens: int = 0,
    context_window: int = 0,
) -> dict:
    """Park agent on session limit: health, control line, wake plan.

    Messages keep landing in inbox/doorbells while parked (no continue thrash
    once delay_until_event or long delay is set by caller).

    Returns dict with delay_s, reset_unix, park_path for turn_engine.
    """
    from asdaaas import agent_dir, write_health, write_conversation, schedule_self_restart

    adir = agent_dir(agent_name, env=env)
    park = write_park_state(adir, info)

    reset_s = seconds_until_reset(info)
    if reset_s is None:
        # Unknown reset: park 1h then retry wake (better than STUCK forever)
        reset_s = 3600.0
        detail = f"session_limited (reset unknown; retry in {int(reset_s)}s)"
    else:
        detail = f"session_limited (wake in {int(reset_s)}s)"

    try:
        write_health(
            agent_name,
            "session_limited",
            detail[:120],
            total_tokens,
            context_window or 0,
            env=env,
        )
    except Exception as e:
        print(f"[session_limit] write_health failed: {e}")

    try:
        write_conversation(
            agent_name,
            "system",
            f"[aa.control] session_limit: {info.raw_snippet[:200] or info.reason} "
            f"| wake_in={int(reset_s)}s | messages will queue until then",
            env=env,
            kind="control",
        )
    except Exception as e:
        print(f"[session_limit] write_conversation failed: {e}")

    print(f"[session_limit] {agent_name}: parked {detail} path={park}")

    # Schedule wake: self-restart at reset so a fresh binary/session can run
    # (account-level limits may still block until true reset — restart is still
    # the right wakeup; agent gets a clear doorbell via post-restart boot).
    try:
        # Cap delay_s for schedule_self_restart sleep — use atime file + main loop
        # for long waits; for short (<2h) schedule restart directly.
        if reset_s <= 7200:
            schedule_self_restart(
                agent_name,
                reason=f"session_limit_wake:{info.reason}",
                delay_s=max(5.0, reset_s),
            )
        else:
            # Long park: write wake_at; main loop / external cron can restart
            print(
                f"[session_limit] long park {reset_s:.0f}s — "
                f"use session_limit.json reset_unix for wake (no auto-restart >2h)"
            )
    except Exception as e:
        print(f"[session_limit] schedule wake failed: {e}")

    return {
        "delay_s": reset_s,
        "reset_unix": info.reset_unix,
        "park_path": str(park),
        "detail": detail,
    }
