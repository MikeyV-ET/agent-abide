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
    # Claude Code's real wording: "resets 8:40pm (America/Los_Angeles)".
    # No "at", minutes optional, zone given as an IANA name in parentheses.
    re.compile(
        r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b"
        r"(?:\s*\(([A-Za-z_]+(?:/[A-Za-z_+\-]+)+)\))?",
        re.I,
    ),
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


def _next_wall_clock(hour: int, minute: int, ampm: str, zone: Optional[str],
                     now: float) -> Optional[float]:
    """Next occurrence of hour:minute in `zone` (local clock if unknown/absent)."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    tz = None
    if zone:
        try:
            tz = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            tz = None
    base = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now).astimezone()
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate = candidate + timedelta(days=1)
    return candidate.timestamp()


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

    m = RESET_PATTERNS[4].search(text)  # resets 8:40pm (America/Los_Angeles)
    if m:
        got = _next_wall_clock(
            int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower(),
            m.group(4), now,
        )
        if got is not None:
            return got

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
    """Write/update park file. **Preserves original parked_at** if already parked.

    Astro 2026-09-21: re-park at boot overwrote T1 to "now", so wake notice
    said away 9s instead of ~7h. T1 must be first park time.
    """
    path = park_path(agent_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    parked_at = now
    parked_at_iso = datetime.now(timezone.utc).isoformat()
    existing = read_park_state(agent_dir)
    if existing and existing.get("status") == "session_limited":
        if existing.get("parked_at") is not None:
            try:
                parked_at = float(existing["parked_at"])
            except (TypeError, ValueError):
                pass
        if existing.get("parked_at_iso"):
            parked_at_iso = str(existing["parked_at_iso"])
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
        "parked_at": parked_at,
        "parked_at_iso": parked_at_iso,
        "updated_at": now,
        "updated_at_iso": datetime.now(timezone.utc).isoformat(),
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



def should_hold_park(agent_dir: Path, *, now: Optional[float] = None) -> bool:
    """True while session_limit.json says we are still before reset.

    During hold: delay loops must NOT interrupt on doorbell/adapter message
    (those queue; sending into a hard limit is the bug Astro measured).
    Shutdown still wins at the caller.
    """
    now = now if now is not None else time.time()
    park = read_park_state(agent_dir)
    if not park or park.get("status") != "session_limited":
        return False
    reset_u = park.get("reset_unix")
    if reset_u is None:
        # Unknown reset: hold until file cleared (wake/retry path clears it).
        return True
    try:
        return float(reset_u) > now
    except (TypeError, ValueError):
        return True


def park_remaining_s(agent_dir: Path, *, now: Optional[float] = None) -> Optional[float]:
    now = now if now is not None else time.time()
    park = read_park_state(agent_dir)
    if not park:
        return None
    reset_u = park.get("reset_unix")
    if reset_u is None:
        return None
    try:
        return max(0.0, float(reset_u) - now)
    except (TypeError, ValueError):
        return None


def count_queued_inputs(agent_dir: Path) -> dict:
    """Best-effort count of human/ops input waiting while parked (not continues)."""
    adir = Path(agent_dir)
    n_bells = 0
    n_continue = 0
    bell_dir = adir / "doorbells"
    if bell_dir.is_dir():
        for f in bell_dir.glob("*.json"):
            name = f.name
            if name.startswith("cont_"):
                n_continue += 1
            else:
                n_bells += 1
    n_adapter = 0
    adapters = adir / "adapters"
    if adapters.is_dir():
        for inbox in adapters.glob("*/inbox"):
            if inbox.is_dir():
                n_adapter += sum(1 for p in inbox.iterdir() if p.is_file())
        # localmail payloads sometimes live under adapters/localmail/
        for pat in ("**/inbox/*.json", "**/payloads/*.json"):
            pass
    return {
        "doorbells": n_bells,
        "continues": n_continue,
        "adapter_files": n_adapter,
        "total": n_bells + n_adapter,
    }


def format_wake_notice(park: dict, *, queued: Optional[dict] = None,
                       now: Optional[float] = None) -> str:
    """Agent-facing wake text: T1→T2, reset clock, queue depth, memory nudge."""
    now = now if now is not None else time.time()
    queued = queued or {}
    t1 = park.get("parked_at_iso") or park.get("parked_at")
    t2 = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()
    reset_iso = park.get("reset_iso")
    reset_u = park.get("reset_unix")
    if not reset_iso and reset_u:
        try:
            reset_iso = datetime.fromtimestamp(float(reset_u), tz=timezone.utc).isoformat()
        except (TypeError, ValueError):
            reset_iso = None
    # local-ish wall clock string if we have reset_unix
    reset_local = None
    if reset_u:
        try:
            reset_local = datetime.fromtimestamp(float(reset_u)).astimezone().strftime(
                "%a %b %d %I:%M%p %Z"
            )
        except (TypeError, ValueError, OSError):
            reset_local = None
    n = int(queued.get("total") or 0)
    n_b = int(queued.get("doorbells") or 0)
    n_a = int(queued.get("adapter_files") or 0)
    lines = [
        "[aa.control] session_limit cleared — back online.",
        f"Parked T1={t1} → wake T2={t2}.",
    ]
    if reset_iso or reset_local:
        lines.append(
            f"Parsed reset was {reset_local or reset_iso}"
            + (f" (utc {reset_iso})." if reset_local and reset_iso else ".")
        )
    else:
        lines.append("Parsed reset was unknown (retry/wake path).")
    lines.append(
        f"Queued while parked: {n} item(s) "
        f"(doorbells={n_b}, adapter_inbox_files={n_a}) — process them before new work."
    )
    lines.append(
        "Memory: call memory_query (or memory_recall) on your conversation record "
        "to recover what was said during the park window and any decisions you missed; "
        "the control line is not a full transcript."
    )
    return "\n".join(lines)


def emit_wake_notice(agent_name: str, park: dict, *, env=None) -> str:
    """Write wake control to conversation + a high-priority doorbell for the agent.

    Returns the notice text. Caller clears park state after.
    """
    from asdaaas import agent_dir, write_conversation, queue_continue_doorbell

    adir = agent_dir(agent_name, env=env)
    queued = count_queued_inputs(adir)
    text = format_wake_notice(park, queued=queued)
    try:
        write_conversation(
            agent_name, "system", text, env=env, kind="control",
        )
    except Exception as e:
        print(f"[session_limit] wake conversation write failed: {e}")
    # Dedicated wake doorbell (not a plain continue) so the model sees it as input
    try:
        bell_dir = adir / "doorbells"
        bell_dir.mkdir(parents=True, exist_ok=True)
        import os, tempfile
        bell = {
            "adapter": "session_limit",
            "priority": 3,
            "text": text,
            "source": "session_limit_wake",
            "ts": time.time(),
        }
        fd, tmp_path = tempfile.mkstemp(
            dir=str(bell_dir), suffix=".tmp", prefix="wake_sesslim_"
        )
        with os.fdopen(fd, "w") as f:
            json.dump(bell, f)
        os.rename(tmp_path, tmp_path.replace(".tmp", ".json"))
    except Exception as e:
        print(f"[session_limit] wake doorbell failed: {e}")
        try:
            queue_continue_doorbell(agent_name, text=text, env=env)
        except Exception:
            pass
    return text


def clear_park_with_wake(agent_name: str, *, env=None) -> Optional[str]:
    """If park file present, emit wake notice and clear. Returns notice or None."""
    from asdaaas import agent_dir

    adir = agent_dir(agent_name, env=env)
    park = read_park_state(adir)
    if not park:
        return None
    notice = emit_wake_notice(agent_name, park, env=env)
    clear_park_state(adir)
    # Restore health so TUI is not stuck on session_limited
    try:
        from asdaaas import write_health
        write_health(agent_name, "idle", "session_limit cleared", 0, 0, env=env)
    except Exception:
        pass
    return notice


def reconcile_session_park(agent_name: str, *, env=None, now: Optional[float] = None) -> dict:
    """Wake+clear if park exists but hold is no longer required.

    Astro 2026-09-21: real limit 08:48 → reset 11:40; park file still present
    at 15:39 with no wake notice. Long parks (>2h) skipped self-restart and
    relied on one in-process delay; process restart or a missed expiry left a
    stale file. Call this on boot and every idle loop tick.
    """
    from asdaaas import agent_dir

    now = now if now is not None else time.time()
    adir = agent_dir(agent_name, env=env)
    park = read_park_state(adir)
    if not park or park.get("status") != "session_limited":
        return {"parked": False}
    if should_hold_park(adir, now=now):
        rem = park_remaining_s(adir, now=now)
        return {
            "parked": True,
            "holding": True,
            "remaining_s": rem,
            "reset_unix": park.get("reset_unix"),
        }
    # Expired / no longer holding — wake
    notice = clear_park_with_wake(agent_name, env=env)
    return {
        "parked": False,
        "holding": False,
        "woke": True,
        "notice": notice,
        "stale_park": park,
    }


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

    reset_s = seconds_until_reset(info)
    if reset_s is not None and reset_s <= 0:
        # Limit text with reset already past (or clock skew). Do not re-park
        # with parked_at=now — that produced Astro's bogus T1 at boot.
        print(
            f"[session_limit] {agent_name}: limit noted but reset already past "
            f"(reset_s={reset_s:.0f}) — reconcile instead of re-park"
        )
        rec = reconcile_session_park(agent_name, env=env)
        return {
            "delay_s": 0.0,
            "delay_s_full": 0.0,
            "reset_unix": info.reset_unix,
            "park_path": str(park_path(adir)),
            "detail": "session_limit reset already past",
            "parked": False,
            "already_expired": True,
            "reconcile": rec,
        }

    park = write_park_state(adir, info)

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

    # Always schedule a detached self-restart at reset (no 2h cap).
    # Astro's 08:48→11:40 park was 10266s; skipping restart left wake solely on
    # in-process delay, which did not clear the park after reset.
    # In-process delay (caller) is a second path; restart is the reliable one.
    try:
        schedule_self_restart(
            agent_name,
            reason=f"session_limit_wake:{info.reason}",
            delay_s=max(5.0, float(reset_s)),
        )
        print(
            f"[session_limit] wake restart scheduled in {reset_s:.0f}s "
            f"(reset_unix={info.reset_unix})"
        )
    except Exception as e:
        print(f"[session_limit] schedule wake failed: {e}")

    # In-process delay chunks (max 10m): each expiry re-checks reset_unix via
    # reconcile / should_hold. Full wait is owned by schedule_self_restart above.
    chunk = min(float(reset_s), 600.0)
    return {
        "delay_s": chunk,
        "delay_s_full": reset_s,
        "reset_unix": info.reset_unix,
        "park_path": str(park),
        "detail": detail,
    }
