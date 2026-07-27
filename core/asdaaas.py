#!/usr/bin/env python3
"""
asdaaas.py — ASDAAAS: Agent Self-Directed Attention and Awareness Architecture System
======================================================================================
One instance per agent. Owns exclusive stdin/stdout pipes to a grok agent stdio subprocess.
Dumb pipe + doorbell panel. Does not filter, suppress, or broadcast.

Responsibilities:
  - Spawn and manage grok agent stdio subprocess
  - Poll adapter inboxes for inbound messages (per awareness file)
  - Pipe messages to agent via stdin, collect response from stdout
  - Capture both speech (agent_message_chunk) and thoughts (agent_thought_chunk)
  - Route speech and thoughts independently based on split gaze file
  - Extract totalTokens from result _meta, write to health file
  - Deliver doorbells from adapters to agent stdin (priority-ordered)
  - Watch command file for adapter commands (e.g., /compact from session adapter)
  - Self-instrumentation (profiling, health heartbeat)

Does NOT:
  - Filter or suppress content (adapter responsibility)
  - Broadcast to other agents (adapter responsibility)
  - Decide what's worth sending (adapter responsibility)

Usage:
    python3 asdaaas.py --agent Trip --session <session-id> --cwd ~/agents/Trip
    python3 asdaaas.py --agent Test   # new session
"""

import asyncio
import datetime
import json
import os
import secrets
import signal
import sys
import time
import argparse
import tempfile
from pathlib import Path
from agent_backend import AgentBackend, ResponseResult, TurnCancelled
from grok_backend import GrokBackend
from permission_handler import (
    write_permission_request, read_decision, archive_request,
)

# Graceful shutdown flag — set by SIGTERM/SIGINT or "shutdown" command
_shutdown_requested = False

from typing import Optional

try:
    from asdaaas_config import config
except ModuleNotFoundError:
    import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'core'))
    from asdaaas_config import config

from asdaaas_env import AsdaaasEnv
from turn_engine import TurnEngine

ASDAAAS_DIR = config.asdaaas_dir
ADAPTERS_DIR = config.adapters_dir
AGENTS_HOME_DIR = config.agents_home

# Legacy compat aliases
HUB_DIR = config.hub_dir
AGENTS_DIR = ASDAAAS_DIR / "agents"
INBOX_DIR = config.inbox_dir
OUTBOX_DIR = config.outbox_dir


def agent_dir(agent_name, env: Optional[AsdaaasEnv] = None):
    """Return the per-agent runtime directory."""
    env = env or AsdaaasEnv.from_config()
    return env.agents_home / agent_name / "asdaaas"

# ============================================================================
# TUNABLE CONSTANTS — collected here for discoverability (structural-pattern-recognition)
# ============================================================================
CONTEXT_WINDOW = 200000             # default, updated from capabilities if available
COMPACTION_THRESHOLD = 0.85         # auto-compaction fires at this fraction of context_window
COMPACTION_COOLDOWN_TURNS = 2       # turns after compaction before manual compact is available
COMPACT_CONFIRM_MAX_TURNS = 3       # turns agent has to confirm compaction before it expires
EMPTY_DOORBELL_BACKOFF_AFTER = 3    # consecutive empty doorbell responses before backoff kicks in
EMPTY_DOORBELL_BACKOFF_PER = 60     # seconds per consecutive empty response
EMPTY_DOORBELL_BACKOFF_MAX = 600    # max backoff seconds
DELAY_POLL_INTERVAL = 0.25          # seconds between checks during delay loop
IDLE_POLL_INTERVAL = 0.25           # seconds between main loop iterations when idle
CONTINUE_DOOM_CHECK_AFTER = 5      # check chat_history for doom_loop corruption after this many empties
CONTINUE_MAX_CONSECUTIVE = 20      # hard cap: stop all continues after this many empties
DOORBELL_MAX_DELIVERIES = 10       # safety cap: auto-expire any doorbell after this many deliveries
DEFAULT_COMPACTION_INSTRUCTIONS = (
    "Preserve: agent identity, corrections log, all pending work items with status, "
    "key file paths, recent commits with hashes, open issues, standing instructions, "
    "and any active conversation context. Omit completed work details unless referenced by pending items."
)
# ============================================================================

RUNNING_AGENTS_FILE = config.running_agents_file

# Timezone for human-readable times in agent-facing output
_AGENT_TZ = None
try:
    from zoneinfo import ZoneInfo
    _tz_name = getattr(config, "timezone", None)
    if _tz_name:
        _AGENT_TZ = ZoneInfo(_tz_name)
except Exception:
    pass


def human_time(epoch_ts=None):
    """Format a timestamp as human-readable local time (e.g. 'Sat May 16 23:03 PDT').

    Uses the timezone from config. Falls back to system local time.
    If epoch_ts is None, uses current time.
    """
    from datetime import datetime
    if epoch_ts is None:
        if _AGENT_TZ:
            return datetime.now(_AGENT_TZ).strftime("%a %b %d %H:%M %Z")
        return time.strftime("%a %b %d %H:%M %Z")
    if _AGENT_TZ:
        return datetime.fromtimestamp(epoch_ts, tz=_AGENT_TZ).strftime("%a %b %d %H:%M %Z")
    return time.strftime("%a %b %d %H:%M %Z", time.localtime(epoch_ts))


def _log_startup_event(agent_name, step, status, detail=""):
    """Append a structured startup event to startup_history.jsonl.

    Called at each startup milestone so failed attempts leave evidence.
    """
    history_file = agent_dir(agent_name) / "startup_history.jsonl"
    try:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "agent": agent_name,
            "step": step,
            "status": status,
            "detail": detail,
            "pid": os.getpid(),
        }
        with open(history_file, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _register_running_agent(agent_name, home_path, env: Optional[AsdaaasEnv] = None):
    """Register this agent in running_agents.json so adapters can find it."""
    env = env or AsdaaasEnv.from_config()
    env.asdaaas_dir.mkdir(parents=True, exist_ok=True)
    agents = load_running_agents()
    agents[agent_name] = {"home": home_path}
    tmp = str(RUNNING_AGENTS_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(agents, f, indent=2)
    os.rename(tmp, str(RUNNING_AGENTS_FILE))


def load_running_agents():
    """Load running_agents.json. Returns dict mapping agent name -> {"home": path}."""
    try:
        with open(RUNNING_AGENTS_FILE) as f:
            data = json.load(f)
        # Handle legacy list format: ["Cinco", "Trip", "Q"]
        if isinstance(data, list):
            return {}
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_agent_home(agent_name):
    """Get an agent's home directory from running_agents.json.
    Returns Path or None if agent not registered."""
    agents = load_running_agents()
    entry = agents.get(agent_name)
    if entry:
        return Path(entry["home"])
    return None

# ============================================================================
# PROFILING
# ============================================================================

class MessageTimer:
    """Per-message profiling. Tracks each stage of message processing."""
    def __init__(self, agent_name, msg_id=""):
        self.agent = agent_name
        self.msg_id = msg_id
        self.marks = {}
        self.mark("inbox_pickup")

    def mark(self, label):
        self.marks[label] = time.monotonic()

    def elapsed(self, start_label, end_label):
        s = self.marks.get(start_label)
        e = self.marks.get(end_label)
        if s is not None and e is not None:
            return round((e - s) * 1000)  # ms
        return None

    def summary(self):
        stages = [
            ("queue_wait", "inbox_pickup", "prompt_sent"),
            ("agent_think", "prompt_sent", "first_chunk"),
            ("streaming", "first_chunk", "prompt_complete"),
            ("outbox_write", "prompt_complete", "outbox_done"),
            ("total", "inbox_pickup", "outbox_done"),
        ]
        result = {}
        for name, start, end in stages:
            v = self.elapsed(start, end)
            if v is not None:
                result[name] = v
        if "total" not in result:
            last_mark = max(self.marks.values()) if self.marks else None
            first_mark = self.marks.get("inbox_pickup")
            if first_mark and last_mark:
                result["total"] = round((last_mark - first_mark) * 1000)
        return result

    def log_line(self):
        s = self.summary()
        parts = [f"{k}={v}ms" for k, v in s.items()]
        return f"[profile] {self.agent} msg={self.msg_id}: {' | '.join(parts)}"


def write_profile(agent_name, timer, env=None):
    profile_dir = agent_dir(agent_name, env=env) / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    summary = timer.summary()
    entry = {
        "agent": agent_name,
        "msg_id": timer.msg_id,
        "ts": time.time(),
        "stages_ms": summary,
    }
    log_path = profile_dir / f"{agent_name}.jsonl"
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    latest_path = profile_dir / f"{agent_name}_latest.json"
    tmp = str(latest_path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(entry, f)
    os.rename(tmp, str(latest_path))


# ============================================================================
# HEALTH
# ============================================================================

def _capture_code_version():
    """Capture git commit hash at import time (not at first use)."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).parent), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"

_code_version = _capture_code_version()

def get_code_version():
    """Get the git commit hash captured when asdaaas was loaded."""
    return _code_version


_current_model_id = "unknown"
_current_session_id = None
_current_backend_type = "grok"

def write_health(agent_name, status, detail="", total_tokens=0, context_window=CONTEXT_WINDOW, env=None,
                  observer_state=None):
    agent_dir(agent_name, env=env).mkdir(parents=True, exist_ok=True)
    health = {
        "agent": agent_name,
        "status": status,
        "detail": detail,
        "ts": time.time(),
        "pid": os.getpid(),
        "totalTokens": total_tokens,
        "contextWindow": context_window,
        "last_activity": time.time(),
        "code_version": get_code_version(),
        "model": _current_model_id,
        "session_id": _current_session_id,
        "backend": _current_backend_type,
    }
    if observer_state is not None:
        health["observer"] = {
            "state": observer_state.get("state"),
            "since": observer_state.get("since"),
            "written_at": observer_state.get("written_at"),
        }
    path = agent_dir(agent_name, env=env) / "health.json"
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(health, f)
    os.rename(tmp, str(path))


# ---------------------------------------------------------------------------
# Universal conversation log
# ---------------------------------------------------------------------------
# Single JSONL file per agent. Backend-agnostic. Frontends read only this.
# Schema: {"ts": ISO8601, "role": "user"|"assistant"|"thinking"|"system",
#          "content": str, "seq": int}

_conv_seq = 0

def write_conversation(agent_name, role, content, env=None):
    """Append one line to ~/agents/<Name>/asdaaas/conversation.jsonl."""
    global _conv_seq
    if not content or not content.strip():
        return
    conv_dir = agent_dir(agent_name, env=env)
    conv_dir.mkdir(parents=True, exist_ok=True)
    path = conv_dir / "conversation.jsonl"
    entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "role": role,
        "content": content.strip(),
        "seq": _conv_seq,
    }
    _conv_seq += 1
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def get_compaction_instructions(agent_name, env=None):
    """Return compaction instructions for the given agent.

    Priority: per-agent file > default constant.
    Per-agent file: ~/agents/<Name>/asdaaas/compaction_instructions.txt
    """
    agent_file = agent_dir(agent_name, env=env) / "compaction_instructions.txt"
    try:
        return agent_file.read_text().strip()
    except (FileNotFoundError, OSError):
        return DEFAULT_COMPACTION_INSTRUCTIONS


def context_left_tag(total_tokens, context_window, turns_since_compaction=None, gaze=None,
                     reasoning_effort_info=None):
    """Format a compact context-remaining tag for prompt injection.
    
    Reports tokens remaining before compaction (85% of context_window),
    not before the theoretical maximum. This is the real usable budget.
    
    Includes compaction status and gaze:
      [Context left 89k till autocompaction | just compacted | irc/pm:eric]
      [Context left 85k till autocompaction | compacted 1 turn ago | irc/#standup]
      [Context left 80k till autocompaction | compaction available | slack/#general]
    
    Returns empty string if context_window is 0 or total_tokens is 0.
    """
    if context_window <= 0 or total_tokens <= 0:
        return ""
    usable = int(context_window * COMPACTION_THRESHOLD)
    remaining = usable - total_tokens
    if remaining < 0:
        remaining = 0
    k = remaining / 1000
    if k >= 10:
        left_str = f"{int(k)}k"
    else:
        left_str = f"{k:.1f}k"
    
    parts = [f"Context left {left_str} till autocompaction"]
    
    if turns_since_compaction is not None:
        if turns_since_compaction == 0:
            parts.append("just compacted")
        elif turns_since_compaction < COMPACTION_COOLDOWN_TURNS:
            parts.append(f"compacted {turns_since_compaction} turn ago")
        else:
            parts.append("compaction available")
    
    if reasoning_effort_info:
        level, remaining = reasoning_effort_info
        if remaining <= 2:
            parts.append(f"reasoning:{level} ({remaining} turn{'s' if remaining != 1 else ''} left, send reasoning_effort to renew)")
        else:
            parts.append(f"reasoning:{level}")

    if gaze is not None:
        parts.append(gaze_label(gaze))
    
    parts.append(human_time())
    
    return "\n[" + " | ".join(parts) + "]"


# ============================================================================
# GAZE (split: speech + thoughts)
# ============================================================================

def read_gaze(agent_name, env=None):
    """Read split gaze file. Returns {"speech": {...}, "thoughts": {...} or None}."""
    gaze_file = agent_dir(agent_name, env=env) / "gaze.json"
    try:
        with open(gaze_file) as f:
            gaze = json.load(f)
        # Support split format
        if "speech" in gaze:
            return gaze
        # Legacy format: treat as speech-only, no thoughts
        return {"speech": {"target": gaze.get("target", "irc"), "params": gaze.get("params", {})}, "thoughts": None}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"speech": {"target": "irc", "params": {"room": "#standup"}}, "thoughts": None}


def write_gaze(agent_name, gaze):
    """Write gaze.json atomically."""
    agent_dir(agent_name).mkdir(parents=True, exist_ok=True)
    gaze_file = agent_dir(agent_name) / "gaze.json"
    tmp = str(gaze_file) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(gaze, f)
    os.rename(tmp, str(gaze_file))


def _build_gaze(cmd):
    """Build a gaze dict from a command. Returns None if invalid.
    
    Supported forms:
      {"action": "gaze", "adapter": "irc", "room": "#meetingroom1"}
      {"action": "gaze", "adapter": "irc", "pm": "eric"}
      {"action": "gaze", "adapter": "irc", "room": "#standup", "thoughts": "#sr-thoughts"}
      {"action": "gaze", "adapter": "tui"}  -- non-IRC adapter (no room needed)
      {"action": "gaze", "off": true}  -- clear gaze
    """
    if cmd.get("off"):
        return {"speech": None, "thoughts": None}
    
    adapter = cmd.get("adapter")
    if not adapter:
        return None
    
    # Build room from either "room" or "pm" key
    room = cmd.get("room")
    pm = cmd.get("pm")
    if room:
        params = {"room": room}
    elif pm:
        params = {"room": f"pm:{pm}", "pm": pm}
    else:
        # Non-IRC adapters (tui, arena, etc.) don't need a room
        params = {}
    
    speech = {"target": adapter, "params": params}
    
    # Optional thoughts target
    thoughts = None
    thoughts_room = cmd.get("thoughts")
    if thoughts_room:
        thoughts = {"target": adapter, "params": {"room": thoughts_room}}
    
    return {"speech": speech, "thoughts": thoughts}


# ============================================================================
# GAZE MATCHING (inbound filtering)
# ============================================================================
#
# Convention: every adapter puts a "room" key in both places:
#   - Gaze params:  {"target": "irc", "params": {"room": "#standup"}}
#   - Message meta: {"adapter": "irc", "meta": {"room": "#standup"}}
#
# asdaaas compares gaze.speech.params.room to msg.meta.room.
# The adapter defines what "room" means:
#   IRC:   "#standup", "pm:eric"
#   Slack: "#general", "dm:eric"
#   Mesh:  "Jr"
#
# asdaaas does NOT interpret room values. It just compares strings.
# ============================================================================

def gaze_label(gaze):
    """Format gaze speech target as compact label for prompt injection.
    
    Returns e.g. "irc/pm:eric", "irc/#standup", "slack/#general", or "none".
    """
    adapter, room = get_room(gaze)
    if adapter is None:
        return "none"
    if room is None:
        return adapter
    return f"{adapter}/{room}"


def get_room(gaze):
    """Extract the room from a gaze's speech target. Returns (adapter, room) tuple.
    
    Handles both canonical form {"room": "pm:eric"} and legacy form {"pm": "eric"}.
    """
    speech = gaze.get("speech")
    if speech is None:
        return None, None
    params = speech.get("params", {})
    room = params.get("room")
    if room is None and "pm" in params:
        room = f"pm:{params['pm']}"
    return speech.get("target"), room


def get_msg_room(msg):
    """Extract the room from a message. Returns (adapter, room) tuple."""
    return msg.get("adapter", "unknown"), msg.get("meta", {}).get("room")


def matches_gaze(msg, gaze):
    """Check if an inbound message matches the agent's current gaze target.
    
    Gaze defines the room. A message matches if it comes from the same
    adapter AND the same room.
    
    Adapter-agnostic: asdaaas compares the "room" key in gaze params
    against the "room" key in message meta. The adapter defines what
    room means.
    
    Returns True if the message is "in the room", False if it's background.
    """
    gaze_adapter, gaze_room = get_room(gaze)
    if gaze_adapter is None:
        return False  # no gaze = nothing matches
    
    msg_adapter, msg_room = get_msg_room(msg)
    
    # Operator TUI is always foreground when any gaze is active.
    # The TUI is the operator's direct interface — never background it.
    if msg_adapter == "tui":
        return True
    
    # Adapter must match
    if msg_adapter != gaze_adapter:
        return False
    
    # If gaze has no room specified, match everything on this adapter
    if gaze_room is None:
        return True
    
    # If message has no room, it doesn't match a specific room gaze
    if msg_room is None:
        return False
    
    return msg_room == gaze_room


def get_background_mode(msg, awareness):
    """Determine background mode for a message that doesn't match gaze.
    
    Checks background_channels dict first, then falls back to background_default.
    Keys in background_channels are room values (adapter-defined strings).
    Returns one of: "doorbell", "pending", "drop".
    """
    bg_channels = awareness.get("background_channels", {})
    bg_default = awareness.get("background_default", "pending")
    
    _, msg_room = get_msg_room(msg)
    if msg_room:
        return bg_channels.get(msg_room, bg_default)
    return bg_default


def format_background_doorbell(msg, agent_name=None, env: Optional[AsdaaasEnv] = None):
    """Format a background message as a doorbell notification.
    
    When text exceeds 120 chars and agent_name is provided, the full message
    is stored in a payload file and the doorbell includes the path for retrieval.
    """
    sender = msg.get("from", "unknown")
    adapter = msg.get("adapter", "unknown")
    text = msg.get("text", "")
    _, room = get_msg_room(msg)
    
    payload_hint = ""
    if len(text) > 120 and agent_name:
        env = env or AsdaaasEnv.from_config()
        payload_dir = env.agents_home / agent_name / "asdaaas" / "adapters" / adapter / "payloads"
        payload_dir.mkdir(parents=True, exist_ok=True)
        msg_id = msg.get("id", secrets.token_hex(8))
        payload_path = payload_dir / f"{msg_id}.json"
        try:
            fd, tmp = tempfile.mkstemp(dir=str(payload_dir), suffix=".tmp", prefix="bg_")
            with os.fdopen(fd, "w") as f:
                json.dump(msg, f, indent=2)
            os.rename(tmp, str(payload_path))
            size_kb = len(text) / 1024
            approx_tokens = len(text) // 4
            payload_hint = f"\n(Full message: cat {payload_path} \u2014 {size_kb:.1f}KB, ~{approx_tokens} tokens)"
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
    
    summary = text[:120] + "..." if len(text) > 120 else text
    
    if room:
        return f"[background] {sender} in {room} (reply_via={adapter} outbox): {summary}{payload_hint}"
    else:
        return f"[background] {sender} (via {adapter}, reply_via={adapter} outbox): {summary}{payload_hint}"


MIDTURN_GRACE_SECONDS = 30  # grace window after doorbell/background work

def _is_midturn_message(msg, last_response_ts, last_was_foreground=True, last_activity_ts=0.0):
    """Check if a message was sent while the agent was busy with other work.

    Returns True if the message timestamp predates the effective "agent was
    working until" time, meaning the sender wrote it during the agent's
    previous turn and is NOT responding to what the agent just said.

    The effective timestamp is max(last_response_ts, last_activity_ts).
    last_activity_ts comes from updates.jsonl frame activity — when
    collect_response hits wall_clock_timeout and resets last_response_ts,
    last_activity_ts still reflects ongoing tool calls / speech chunks.

    Grace periods:
    - When last_activity_ts > last_response_ts, the agent was still working
      after collect_response returned (wall clock timeout). Apply grace to
      catch messages arriving during ongoing work.
    - When last activity was non-foreground (doorbells, background), apply
      grace because the user wasn't interacting directly.

    Both timestamps are epoch floats."""
    if last_response_ts is None:
        return False
    msg_ts = msg.get("_received_ts") or msg.get("ts")
    if not isinstance(msg_ts, (int, float)):
        return False
    effective_ts = max(last_response_ts, last_activity_ts or 0.0)
    # Grace only in wall clock timeout case: agent was still actively working
    # (tool calls / speech) after collect_response returned. Non-foreground
    # turns (doorbells) no longer get grace — caused false positives on
    # between-turns messages (issue_0035).
    if last_activity_ts and last_activity_ts > last_response_ts:
        grace = MIDTURN_GRACE_SECONDS
    else:
        grace = 0
    return msg_ts < effective_ts + grace


def _midturn_flag(msg):
    """Format the [sent during your previous turn] flag with send timestamp."""
    msg_ts = msg.get("_received_ts") or msg.get("ts")
    if isinstance(msg_ts, (int, float)):
        return f" [sent during your previous turn, {human_time(msg_ts)}]"
    return " [sent during your previous turn]"


class PendingQueue:
    """Queue for messages that arrive on background rooms in 'pending' mode.
    
    Messages are stored per room key. When gaze changes to match a room,
    the queued messages are delivered.
    """
    
    def __init__(self):
        self._queue = {}  # {room: [msg, msg, ...]}
    
    def add(self, msg):
        """Add a message to the pending queue."""
        _, room = get_msg_room(msg)
        key = room or "_no_room"
        if key not in self._queue:
            self._queue[key] = []
        self._queue[key].append(msg)
    
    def drain_for_gaze(self, gaze):
        """Return and remove all pending messages that match the current gaze.
        
        Called when gaze changes or at the start of each loop iteration
        to check if queued messages should now be delivered.
        """
        _, gaze_room = get_room(gaze)
        if gaze_room and gaze_room in self._queue:
            return self._queue.pop(gaze_room)
        return []
    
    @property
    def total(self):
        return sum(len(v) for v in self._queue.values())


# ============================================================================
# ATTENTION STRUCTURE (expect_response + timeout)
# ============================================================================
#
# Agents declare what they're waiting for by writing attention files.
# asdaaas enforces the boundaries: delivers [RESPONSE] or [TIMEOUT] doorbells.
# Files persist across compaction -- intentionality survives context death.
#
# Path: ~/agents/<agent>/asdaaas/attention/<msg_id>.json
# Matching: FIFO per target agent. First response from target matches oldest
#           pending attention for that target. Responding agent doesn't need
#           to know about the attention -- just responds naturally.
# ============================================================================

def poll_attentions(agent_name, env=None):
    """Read all pending attention declarations for an agent.
    Returns list of attention dicts, sorted by created_at (oldest first = FIFO)."""
    attn_dir = agent_dir(agent_name, env=env) / "attention"
    if not attn_dir.exists():
        return []
    attentions = []
    for f in sorted(attn_dir.glob("*.json")):
        try:
            with open(f) as fh:
                attn = json.load(fh)
            attn["_path"] = str(f)
            attentions.append(attn)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[asdaaas] attention read error: {e}")
    attentions.sort(key=lambda a: a.get("created_at", 0))
    return attentions


def check_attention_timeouts(agent_name, attentions, env=None):
    """Check for expired attentions. Returns list of timeout doorbell dicts.
    Deletes expired attention files."""
    now = time.time()
    timeouts = []
    for attn in attentions:
        if now > attn.get("expires_at", float("inf")):
            target = attn.get("expecting_from", "unknown")
            msg_id = attn.get("msg_id", "unknown")
            timeout_s = attn.get("timeout_s", "?")
            timeouts.append({
                "adapter": "attention",
                "text": f"[TIMEOUT {msg_id}] No response from {target} within {timeout_s}s",
                "priority": 2,
                "msg_id": msg_id,
            })
            # Delete the expired attention file
            try:
                os.unlink(attn["_path"])
                print(f"[asdaaas] TIMEOUT: attention {msg_id} for {target} expired after {timeout_s}s")
            except OSError:
                pass
    return timeouts


def match_attention(agent_name, attentions, sender):
    """Check if a message from sender matches any pending attention.
    Returns the matched attention dict (oldest first = FIFO), or None.
    Does NOT delete the file -- caller does that after delivering the response."""
    for attn in attentions:
        if attn.get("expecting_from", "").lower() == sender.lower():
            return attn
    return None


def resolve_attention(attn, response_text):
    """Create a response doorbell for a matched attention and delete the file.
    Returns a doorbell dict for delivery to the agent."""
    msg_id = attn.get("msg_id", "unknown")
    target = attn.get("expecting_from", "unknown")
    # Truncate response for doorbell (full text available in inbox)
    preview = response_text[:800] + "..." if len(response_text) > 800 else response_text
    
    # Delete the attention file
    try:
        os.unlink(attn["_path"])
        print(f"[asdaaas] RESOLVED: attention {msg_id} from {target}")
    except OSError:
        pass
    
    return {
        "adapter": "attention",
        "text": f"[RESPONSE to {msg_id}] from {target}: {preview}",
        "priority": 2,
        "msg_id": msg_id,
    }


# ============================================================================
# AWARENESS FILE
# ============================================================================

def read_awareness(agent_name, env=None):
    """Read agent awareness file. Returns dict with direct_attach, control_watch, notify_watch."""
    awareness_file = agent_dir(agent_name, env=env) / "awareness.json"
    try:
        with open(awareness_file) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # Default awareness: watch TUI and IRC direct adapters
        return {
            "direct_attach": ["tui", "irc"],
            "control_watch": {},
            "notify_watch": [],
            "accept_from": ["*"],
        }


def write_awareness(agent_name, awareness):
    """Write awareness.json atomically."""
    agent_dir(agent_name).mkdir(parents=True, exist_ok=True)
    awareness_file = agent_dir(agent_name) / "awareness.json"
    tmp = str(awareness_file) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(awareness, f, indent=2)
    os.rename(tmp, str(awareness_file))


def _apply_awareness_command(cmd, current_awareness):
    """Apply an awareness command to the current awareness dict. Returns updated copy.
    
    Supported forms:
      {"action": "awareness", "add": "#meetingroom1", "mode": "doorbell"}
      {"action": "awareness", "remove": "#meetingroom1"}
      {"action": "awareness", "default": "pending"}
      {"action": "awareness", "doorbell_ttl": {"irc": 3, "heartbeat": 1}}
      {"action": "awareness", "attach": "arena"}
      {"action": "awareness", "detach": "arena"}
    
    Returns (updated_awareness, description_string) or (None, error_string).
    """
    awareness = json.loads(json.dumps(current_awareness))  # deep copy
    
    if "add" in cmd:
        channel = cmd["add"]
        mode = cmd.get("mode", "doorbell")
        if mode not in ("doorbell", "pending", "drop"):
            return None, f"invalid mode: {mode}"
        bg = awareness.setdefault("background_channels", {})
        bg[channel] = mode
        return awareness, f"added {channel}={mode}"
    
    if "remove" in cmd:
        channel = cmd["remove"]
        bg = awareness.get("background_channels", {})
        if channel in bg:
            del bg[channel]
            return awareness, f"removed {channel}"
        return awareness, f"{channel} not in background_channels (no-op)"
    
    if "default" in cmd:
        new_default = cmd["default"]
        if new_default not in ("doorbell", "pending", "drop"):
            return None, f"invalid default: {new_default}"
        awareness["background_default"] = new_default
        return awareness, f"default={new_default}"
    
    if "doorbell_ttl" in cmd:
        ttl_updates = cmd["doorbell_ttl"]
        if not isinstance(ttl_updates, dict):
            return None, "doorbell_ttl must be a dict"
        ttl = awareness.setdefault("doorbell_ttl", {})
        ttl.update(ttl_updates)
        return awareness, f"doorbell_ttl updated: {ttl_updates}"
    
    if "attach" in cmd:
        adapter = cmd["attach"]
        da = awareness.setdefault("direct_attach", [])
        if adapter not in da:
            da.append(adapter)
            return awareness, f"direct_attach added: {adapter}"
        return awareness, f"{adapter} already in direct_attach (no-op)"
    
    if "detach" in cmd:
        adapter = cmd["detach"]
        da = awareness.get("direct_attach", [])
        if adapter in da:
            da.remove(adapter)
            return awareness, f"direct_attach removed: {adapter}"
        return awareness, f"{adapter} not in direct_attach (no-op)"
    
    return None, "no recognized awareness sub-command"


# ============================================================================
# PER-ADAPTER INBOX POLLING
# ============================================================================

def poll_adapter_inboxes(agent_name, awareness, env=None):
    """Poll all adapter inboxes that the agent is aware of.
    Returns list of messages from all watched adapters.
    DESTRUCTIVE: deletes inbox files after reading. Use has_pending_adapter_messages()
    for non-destructive checks (e.g. during delay interruption)."""
    messages = []
    
    # Poll direct adapter inboxes (agent-centric: ~/agents/<name>/asdaaas/adapters/<adapter>/inbox/)
    for adapter in awareness.get("direct_attach", []):
        inbox = agent_dir(agent_name, env=env) / "adapters" / adapter / "inbox"
        if not inbox.exists():
            continue
        for f in sorted(inbox.glob("*.json")):
            try:
                mtime = os.path.getmtime(f)
                with open(f) as fh:
                    msg = json.load(fh)
                msg["_received_ts"] = mtime
                messages.append(msg)
                os.unlink(f)
            except (json.JSONDecodeError, OSError) as e:
                print(f"[asdaaas] adapter inbox read error ({adapter}): {e}")
    
    # Poll notify adapter inboxes (for doorbell-only adapters like localmail)
    # Note: for notify adapters, we don't pipe content — we ring the bell
    # But the doorbell itself comes through the doorbell directory, not here
    # This is for future use if needed
    
    return messages


def has_pending_adapter_messages(agent_name, awareness, env=None):
    """Non-destructive check: are there any messages in adapter inboxes?
    Used during delay interruption checks where we need to detect new
    messages without consuming them. The actual poll_adapter_inboxes()
    call happens in the main loop after the delay breaks."""
    for adapter in awareness.get("direct_attach", []):
        inbox = agent_dir(agent_name, env=env) / "adapters" / adapter / "inbox"
        if not inbox.exists():
            continue
        if any(inbox.glob("*.json")):
            return True
    return False


async def run_delay_loop(agent_name, delay_seconds, awareness, poll_interval=DELAY_POLL_INTERVAL, env=None):
    """Run the delay loop, checking for external events every poll_interval seconds.
    
    Checks doorbells and adapter messages only — NOT pending commands.
    Commands are processed at the top of each main loop iteration (step 1a).
    The agent's own delay commands must not interrupt the current delay
    (bug: agent writes multiple commands per turn, each found by
    has_pending_commands() and killing the active delay loop).
    Shutdown is handled via _shutdown_requested (SIGTERM/SIGINT handler).
    
    Returns:
        (interrupted: bool, reason: str)
        - interrupted=True, reason="doorbell" / "adapter_message" / "shutdown"
        - interrupted=False, reason="expired" if the delay expired naturally
    """
    delay_remaining = delay_seconds
    while delay_remaining > 0:
        await asyncio.sleep(min(poll_interval, delay_remaining))
        delay_remaining -= poll_interval
        if _shutdown_requested:
            return True, "shutdown"
        if has_pending_doorbells(agent_name, env=env):
            return True, "doorbell"
        if has_pending_adapter_messages(agent_name, awareness, env=env):
            return True, "adapter_message"
    return False, "expired"


def queue_continue_doorbell(agent_name, text=None, env=None):
    """Queue a [continue] doorbell for the agent, unless one already exists.
    
    If *text* is provided (from a delay command's ``text`` field), it replaces
    the default continue message so the agent receives directed context.

    Returns True if a doorbell was queued, False if one already existed."""
    bell_dir = agent_dir(agent_name, env=env) / "doorbells"
    bell_dir.mkdir(parents=True, exist_ok=True)
    if any(f.name.startswith("cont_") for f in bell_dir.glob("*.json")):
        return False
    bell = {
        "adapter": "continue",
        "priority": 10,
        "text": text or "Your turn ended. You may continue, delay, or stand by.",
        "source": "continue",
        "ts": time.time(),
    }
    fd, tmp_path = tempfile.mkstemp(dir=str(bell_dir), suffix=".tmp", prefix="cont_")
    with os.fdopen(fd, "w") as f:
        json.dump(bell, f)
    os.rename(tmp_path, tmp_path.replace(".tmp", ".json"))
    return True


def write_to_outbox(agent_name, content, gaze_target, content_type="speech", env=None):
    """Write a message to an adapter's per-agent outbox."""
    if gaze_target is None:
        return  # null target = discard

    target = gaze_target.get("target", "irc")
    params = gaze_target.get("params", {})
    
    # Agent-centric outbox: ~/agents/<name>/asdaaas/adapters/<target>/outbox/
    outbox = agent_dir(agent_name, env=env) / "adapters" / target / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)

    msg = {
        "from": agent_name,
        "content_type": content_type,
        "text": content,
    }
    msg.update(params)

    fd, tmp_path = tempfile.mkstemp(dir=str(outbox), suffix=".tmp", prefix="resp_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(msg, f)
        final = tmp_path.replace(".tmp", ".json")
        os.rename(tmp_path, final)
        print(f"[asdaaas] {agent_name} {content_type} -> {target}/{agent_name} ({len(content)} chars)")
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ============================================================================
# STREAMING THOUGHTS -- real-time intermediate speech routing
# ============================================================================

class StreamingThoughts:
    """Accumulates speech chunks and flushes to thoughts outbox on boundaries.
    
    During a tool-heavy turn, the agent emits speech between tool calls:
    "Let me check the tests..." [tool_call: run_terminal_cmd] "23/23 passing..."
    
    This class captures those intermediate chunks and writes them to the
    thoughts gaze target when a tool_call boundary is hit, giving observers
    a real-time view of what the agent is doing.
    
    The final speech still goes through the normal gaze speech routing.
    Streaming thoughts are a parallel channel for live observation.
    
    Usage:
        st = StreamingThoughts(agent_name, gaze)
        speech, thoughts, meta = await collect_response(
            stdout, prompt_id,
            on_speech_chunk=st.on_chunk,
            on_tool_call=st.on_tool_call)
        st.flush()  # flush any remaining chunks after response completes
    """
    
    def __init__(self, agent_name, gaze):
        self.agent_name = agent_name
        self.thoughts_target = gaze.get("thoughts")
        self._buffer = []
        self._chunk_count = 0
    
    def on_chunk(self, text):
        """Called on each agent_message_chunk."""
        self._buffer.append(text)
        self._chunk_count += 1
    
    def on_tool_call(self, title):
        """Called when a tool_call frame arrives -- flush accumulated speech."""
        self.flush(f" [{title}]")
    
    def flush(self, suffix=""):
        """Write accumulated chunks to thoughts outbox and clear buffer."""
        if not self._buffer or not self.thoughts_target:
            self._buffer.clear()
            return
        text = "".join(self._buffer).strip()
        if text:
            write_to_outbox(self.agent_name, text + suffix, self.thoughts_target, "thoughts")
        self._buffer.clear()
    
    @property
    def chunk_count(self):
        return self._chunk_count


# ============================================================================
# INBOX POLLING
# ============================================================================

def poll_inbox(agent_name, env=None):
    """Poll universal inbox for messages addressed to this agent."""
    if not INBOX_DIR.exists():
        return []
    messages = []
    for f in sorted(INBOX_DIR.glob("*.json")):
        try:
            with open(f) as fh:
                msg = json.load(fh)
            if msg.get("to") == agent_name or msg.get("to") == "broadcast":
                messages.append(msg)
                os.unlink(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[asdaaas] inbox read error: {e}")
    return messages


# ============================================================================
# DOORBELL DELIVERY
# ============================================================================

def poll_doorbells(agent_name, awareness=None, env=None):
    """Poll doorbell directory for notifications from adapters.
    
    Doorbells persist on disk until explicitly acked or TTL-expired.
    Each doorbell gets an 'id' (filename stem) and 'delivered_count' 
    (incremented each delivery). TTL is resolved per-source from the
    agent's awareness file doorbell_ttl map.
    
    Returns list of doorbell dicts, sorted by priority (lowest first).
    Expired doorbells are auto-removed and not returned.
    """
    bell_dir = agent_dir(agent_name, env=env) / "doorbells"
    if not bell_dir.exists():
        return []
    
    # Get per-source TTL from awareness
    ttl_map = {}
    if awareness:
        ttl_map = awareness.get("doorbell_ttl", {})
    default_ttl = ttl_map.get("default", 0)  # 0 = persist indefinitely
    
    bells = []
    for f in sorted(bell_dir.glob("*.json")):
        try:
            with open(f) as fh:
                bell = json.load(fh)
            
            # Assign id from filename if not present
            bell_id = bell.get("id", f.stem)
            bell["id"] = bell_id
            
            # Increment delivered_count
            delivered = bell.get("delivered_count", 0) + 1
            bell["delivered_count"] = delivered
            
            # Check TTL expiry
            source = bell.get("source", bell.get("adapter", "unknown"))
            ttl = ttl_map.get(source, default_ttl)
            if ttl > 0 and delivered > ttl:
                # Expired -- remove and skip
                os.unlink(f)
                print(f"[asdaaas] doorbell expired (TTL={ttl}, delivered={delivered}): {bell_id}")
                continue

            # Safety cap: no doorbell survives forever regardless of TTL config
            if delivered > DOORBELL_MAX_DELIVERIES:
                os.unlink(f)
                print(f"[asdaaas] doorbell safety cap (>{DOORBELL_MAX_DELIVERIES} deliveries): {bell_id}")
                continue
            
            # Write back updated delivered_count
            with open(f, "w") as fh:
                json.dump(bell, fh)
            
            bells.append(bell)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[asdaaas] doorbell read error: {e}")
    # Sort by priority (lower number = higher priority, default 5)
    bells.sort(key=lambda b: b.get("priority", 5))
    return bells


def has_pending_doorbells(agent_name, env=None):
    """Check if any doorbell files exist without modifying them.
    Used for delay interruption checks where we need to know if
    events arrived but don't want to increment delivered_count."""
    bell_dir = agent_dir(agent_name, env=env) / "doorbells"
    if not bell_dir.exists():
        return False
    return any(bell_dir.glob("*.json"))


def ack_doorbells(agent_name, handled_ids, env=None):
    """Remove acked doorbells from disk.
    
    Agent writes {"action": "ack", "handled": ["id1", "id2"]} to command file.
    Everything not in handled_ids persists for next delivery.
    """
    bell_dir = agent_dir(agent_name, env=env) / "doorbells"
    if not bell_dir.exists():
        return 0
    removed = 0
    handled_set = set(handled_ids)
    for f in bell_dir.glob("*.json"):
        try:
            with open(f) as fh:
                bell = json.load(fh)
            bell_id = bell.get("id", f.stem)
            if bell_id in handled_set:
                os.unlink(f)
                removed += 1
                print(f"[asdaaas] doorbell acked: {bell_id}")
        except (json.JSONDecodeError, OSError) as e:
            print(f"[asdaaas] doorbell ack error: {e}")
    return removed


def _cleanup_compact_doorbells(agent_name, env=None):
    """Remove all compact_confirm doorbells from disk.
    
    Called after compaction succeeds or the confirmation request expires.
    Without this cleanup, persistent doorbells re-deliver the stale
    compact_confirm prompt to the agent, which interprets it as a new
    request and writes a new compact command -- creating an infinite loop.
    (Bug observed: Q went through 8 cycles of this in Session 40.)
    """
    bell_dir = agent_dir(agent_name, env=env) / "doorbells"
    if not bell_dir.exists():
        return
    removed = 0
    for f in bell_dir.glob("*.json"):
        try:
            with open(f) as fh:
                bell = json.load(fh)
            if bell.get("command") == "compact_confirm":
                os.unlink(f)
                removed += 1
        except (json.JSONDecodeError, OSError):
            pass
    if removed:
        print(f"[asdaaas] Cleaned up {removed} compact_confirm doorbell(s)")


def _cleanup_continue_doorbells(agent_name, env=None):
    """Remove all continue doorbells from disk.

    Called when delay until_event is set.  Continue doorbells persist like
    all doorbells, so setting delay_until_event alone only prevents *new*
    continues from being queued -- it doesn't stop already-queued ones from
    being re-delivered each iteration.  (Bug_0003: Jr saw 50+ re-deliveries.)
    """
    bell_dir = agent_dir(agent_name, env=env) / "doorbells"
    if not bell_dir.exists():
        return
    removed = 0
    for f in bell_dir.glob("cont_*.json"):
        try:
            os.unlink(f)
            removed += 1
        except OSError:
            pass
    if removed:
        print(f"[asdaaas] Cleaned up {removed} continue doorbell(s)")


def _queue_post_compaction_doorbell(agent_name, tokens_before, tokens_after, env=None):
    """Queue a doorbell telling the agent it just came back from compaction.

    After auto-compaction, the binary injects 'Continue... pick up as if the
    break never happened' which conflicts with AGENTS.md boot protocol.  This
    doorbell fires on the next turn to override that and trigger re-orientation.
    """
    bell_dir = agent_dir(agent_name, env=env) / "doorbells"
    bell_dir.mkdir(parents=True, exist_ok=True)
    bell = {
        "adapter": "system",
        "priority": 1,
        "text": (
            f"[Compaction complete. Context reduced from {tokens_before} to {tokens_after} tokens. "
            f"You are resuming from a compacted context. Follow your boot protocol.]"
        ),
        "source": "compaction",
        "ts": time.time(),
    }
    bell_id = f"compact_{int(time.time() * 1000)}"
    fd, tmp_path = tempfile.mkstemp(dir=str(bell_dir), suffix=".tmp", prefix=f"{bell_id}_")
    with os.fdopen(fd, "w") as f:
        json.dump(bell, f)
    os.rename(tmp_path, tmp_path.replace(".tmp", ".json"))
    print(f"[asdaaas] Post-compaction doorbell queued for {agent_name}")


def format_doorbell(bell):
    """Format a doorbell notification for delivery to agent stdin.
    
    Includes doorbell id and delivery count so the agent can ack it
    and knows if this is a re-delivery.
    """
    adapter = bell.get("adapter", "unknown")
    command = bell.get("command", "")
    text = bell.get("text", "")
    bell_id = bell.get("id", "")
    delivered = bell.get("delivered_count", 1)
    
    # Build prefix
    if command:
        prefix = f"[{adapter}:{command}"
    else:
        prefix = f"[{adapter}"
    
    # Add id, delivery info, timestamp, and reply hint
    meta_parts = []
    if bell_id:
        meta_parts.append(f"id={bell_id}")
    if delivered > 1:
        meta_parts.append(f"delivery={delivered}")
    ts = bell.get("ts")
    if ts:
        meta_parts.append(f"ts={human_time(ts)}")
    # Reply hint: tells agent where to send response for non-gaze adapters
    if adapter not in ("session", "operator", "context", "heartbeat", "continue"):
        meta_parts.append(f"reply_via={adapter} outbox")
    
    if meta_parts:
        prefix += f" ({', '.join(meta_parts)})"
    
    suffix = ""
    if bell_id and adapter != "continue":
        suffix = f'\nTo ack: include "ack": ["{bell_id}"] in your next command, or {{"action": "ack", "handled": ["{bell_id}"]}}'

    return f"{prefix}] {text}{suffix}"


# ============================================================================
# COMMAND FILE WATCHER
# ============================================================================

def poll_commands(agent_name, env=None):
    """Poll command queue for commands from adapters or the agent itself.
    E.g., session adapter sends {"action": "compact"}, agent sends {"action": "delay"}.
    
    Reads from two sources (in order):
    1. Legacy commands.json (single file, backward compat)
    2. commands/ directory (queue, sorted by filename = chronological)
    
    DESTRUCTIVE: deletes command files after reading. Use has_pending_commands()
    for non-destructive checks (e.g. during delay interruption).
    
    Returns a list of command dicts (may be empty). Previously returned a single
    dict or None; callers should iterate over the list.
    """
    a_dir = agent_dir(agent_name, env=env)
    a_dir.mkdir(parents=True, exist_ok=True)
    commands = []

    # 1. Legacy single-file (backward compat + migration)
    cmd_file = a_dir / "commands.json"
    if cmd_file.exists():
        try:
            with open(cmd_file) as f:
                cmd = json.load(f)
            os.unlink(cmd_file)
            commands.append(cmd)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[asdaaas] command read error (legacy): {e}")

    # 2. Queue directory
    cmd_dir = a_dir / "commands"
    if cmd_dir.is_dir():
        files = sorted(cmd_dir.glob("*.json"))
        for fp in files:
            try:
                with open(fp) as f:
                    cmd = json.load(f)
                os.unlink(fp)
                commands.append(cmd)
            except (json.JSONDecodeError, OSError) as e:
                print(f"[asdaaas] command read error ({fp.name}): {e}")

    return commands


def has_pending_commands(agent_name):
    """Non-destructive check: are there any commands waiting?
    Used during delay interruption checks.
    Checks both legacy commands.json and commands/ directory."""
    a_dir = agent_dir(agent_name)
    if (a_dir / "commands.json").exists():
        return True
    cmd_dir = a_dir / "commands"
    if cmd_dir.is_dir() and any(cmd_dir.glob("*.json")):
        return True
    return False


def write_command(agent_name, command):
    """Write a command to the agent's command queue.
    
    Creates a timestamped file in commands/ directory.
    This is the preferred way to issue commands — avoids the single-slot
    race condition of writing directly to commands.json.
    """
    a_dir = agent_dir(agent_name)
    cmd_dir = a_dir / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time() * 1000)
    rand = secrets.token_hex(4)
    fp = cmd_dir / f"cmd_{ts}_{rand}.json"
    with open(fp, "w") as f:
        json.dump(command, f)
    return fp


def cancel_turn_flag_path(agent_name):
    """Path to the cancel sentinel file for an agent.
    
    When this file exists, asdaaas will cancel the current turn mid-flight:
    kill the grok process, restart with session/load, deliver a doorbell.
    The file is deleted after the cancel is processed.
    """
    return agent_dir(agent_name) / "cancel_turn.flag"


async def watch_cancel_flag(agent_name, cancel_event, poll_interval=0.5):
    """Background task: poll for cancel_turn.flag and set cancel_event when found."""
    flag = cancel_turn_flag_path(agent_name)
    while True:
        try:
            if flag.exists():
                cancel_event.set()
                return
        except OSError:
            pass
        await asyncio.sleep(poll_interval)


# ============================================================================
# COMMAND WATCHDOG (Phase 4.4 — Dead Adapter Safety Net)
# ============================================================================

class CommandWatchdog:
    """Track pending commands sent to control adapters.
    
    When an agent's response triggers a write to a control adapter's inbox,
    we start a watchdog timer. If no acknowledgment doorbell arrives within
    the timeout, we deliver an error doorbell to the agent so it knows the
    command failed.
    
    Timeouts are configurable per-command, per-adapter, or fall back to 10s default.
    """
    
    def __init__(self, agent_name):
        self.agent = agent_name
        self.pending = {}  # {request_id: {"adapter", "command", "deadline", "text"}}
    
    def track(self, request_id, adapter, command="", timeout=None):
        """Start tracking a command. Returns the request_id."""
        if timeout is None:
            timeout = 10.0  # default
        self.pending[request_id] = {
            "adapter": adapter,
            "command": command,
            "deadline": time.monotonic() + timeout,
            "started": time.time(),
        }
        print(f"[asdaaas] Watchdog: tracking {adapter}:{command} (req={request_id}, timeout={timeout}s)")
        return request_id
    
    def acknowledge(self, request_id):
        """Mark a command as acknowledged. Called when doorbell arrives with matching request_id."""
        if request_id in self.pending:
            cmd = self.pending.pop(request_id)
            print(f"[asdaaas] Watchdog: ack {cmd['adapter']}:{cmd['command']} (req={request_id})")
            return True
        return False
    
    def check_expired(self):
        """Check for timed-out commands. Returns list of expired command dicts."""
        now = time.monotonic()
        expired = []
        expired_ids = []
        for req_id, cmd in self.pending.items():
            if now >= cmd["deadline"]:
                expired.append({
                    "request_id": req_id,
                    "adapter": cmd["adapter"],
                    "command": cmd["command"],
                    "started": cmd["started"],
                })
                expired_ids.append(req_id)
        for req_id in expired_ids:
            del self.pending[req_id]
        return expired
    
    def deliver_timeout_doorbells(self, agent_name):
        """Check for expired commands and write error doorbells for them."""
        expired = self.check_expired()
        for cmd in expired:
            bell_dir = agent_dir(agent_name) / "doorbells"
            bell_dir.mkdir(parents=True, exist_ok=True)
            bell = {
                "adapter": cmd["adapter"],
                "command": cmd["command"],
                "priority": 1,  # high priority — error
                "text": f"TIMEOUT: Command \'{cmd['command']}\' to {cmd['adapter']} "
                        f"did not respond (sent {cmd['started']}). "
                        f"Adapter may be dead or unresponsive.",
                "error": True,
                "request_id": cmd["request_id"],
                "ts": time.time(),
            }
            fd, tmp_path = tempfile.mkstemp(dir=str(bell_dir), suffix=".tmp", prefix="timeout_")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(bell, f)
                final = tmp_path.replace(".tmp", ".json")
                os.rename(tmp_path, final)
                print(f"[asdaaas] Watchdog: TIMEOUT {cmd['adapter']}:{cmd['command']} (req={cmd['request_id']})")
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        return expired


# ============================================================================
# ADAPTER REGISTRATION READER (Phase 7.2)
# ============================================================================

def read_adapter_registrations():
    """Read all adapter registration files from ~/asdaaas/adapters/<name>.json.
    
    Returns dict of {adapter_name: registration_dict}.
    Only returns adapters whose registration file is a direct JSON file
    in the adapters directory (not subdirectories which are inbox/outbox).
    """
    registrations = {}
    if not ADAPTERS_DIR.exists():
        return registrations
    
    for entry in sorted(ADAPTERS_DIR.iterdir()):
        if not entry.is_file() or not entry.name.endswith(".json"):
            continue
        try:
            with open(entry) as f:
                reg = json.load(f)
            name = reg.get("name", entry.stem)
            
            # Check liveness via PID
            pid = reg.get("pid")
            if pid:
                try:
                    os.kill(pid, 0)
                    reg["alive"] = True
                except (OSError, ProcessLookupError):
                    reg["alive"] = False
            else:
                reg["alive"] = False
            
            registrations[name] = reg
        except (json.JSONDecodeError, OSError):
            continue
    
    return registrations


# ============================================================================
# JSON-RPC PROTOCOL
# ============================================================================

DEBUG = os.environ.get("ASDAAAS_DEBUG", "0") == "1"

_rpc_id = 0

def rpc_request(method, params=None):
    global _rpc_id
    _rpc_id += 1
    msg = {"jsonrpc": "2.0", "method": method, "id": _rpc_id}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg) + "\n"

def rpc_notification(method, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg) + "\n"

async def read_frame(stdout):
    chunks = []
    while True:
        try:
            chunk = await stdout.readuntil(b'\n')
            chunks.append(chunk)
            break
        except asyncio.LimitOverrunError as e:
            chunk = await stdout.read(e.consumed)
            chunks.append(chunk)
        except asyncio.IncompleteReadError as e:
            if e.partial:
                chunks.append(e.partial)
            if not chunks:
                return None
            break
    data = b"".join(chunks)
    if not data:
        return None
    return json.loads(data.decode("utf-8").strip())

async def send(stdin, msg):
    stdin.write(msg.encode("utf-8"))
    await stdin.drain()

async def wait_for_response(stdout, expected_id, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            frame = await asyncio.wait_for(
                read_frame(stdout),
                timeout=max(0.1, deadline - time.monotonic())
            )
        except asyncio.TimeoutError:
            break
        if frame is None:
            raise RuntimeError("stdio process closed stdout")
        if frame.get("id") == expected_id:
            return frame
    raise TimeoutError(f"No response for id={expected_id} within {timeout}s")


async def collect_response(stdout, prompt_id, timer=None, timeout=120.0, on_meta=None,
                           keepalive_timeout=30.0, max_wall_clock=600.0,
                           on_speech_chunk=None, on_tool_call=None):
    """Collect agent response. Returns (speech_text, thought_text, result_meta).
    
    speech_text: concatenated agent_message_chunk text
    thought_text: concatenated agent_thought_chunk text
    result_meta: dict with totalTokens, modelId, stopReason from _meta
    on_meta: optional callback(total_tokens) called when streaming _meta arrives,
             enabling real-time health file updates during long responses
    on_speech_chunk: optional callback(text) called on each agent_message_chunk,
             enabling real-time streaming of intermediate speech (between tool calls)
             to a thoughts channel or observer.
    on_tool_call: optional callback(title) called when a tool_call frame arrives,
             enabling the caller to flush/route accumulated speech before the tool runs.
    keepalive_timeout: seconds of silence (no frames) before timing out (default 30s).
             As long as frames keep arriving, we keep reading. This prevents
             tool-heavy turns from being cut off at a fixed wall clock.
    max_wall_clock: absolute maximum seconds to wait (safety net, default 600s).
    
    The 'timeout' parameter is kept for backward compatibility with tests but
    is now used as the keepalive_timeout when keepalive_timeout is not explicitly set.
    """
    speech_chunks = []
    thought_chunks = []
    result_meta = {}
    first_chunk_marked = False
    saw_prompt_complete = False
    pending_tool_calls = set()  # toolCallIds of tools currently executing
    now = time.monotonic()
    last_frame_time = now
    wall_deadline = now + max_wall_clock

    while True:
        # Exit conditions:
        # 1. prompt_complete seen + keepalive fires (response frame didn't arrive)
        # 2. max_wall_clock exceeded (absolute safety net)
        #
        # We do NOT exit on keepalive alone before prompt_complete. The model
        # may be reasoning (planning, thinking) between speech chunks with no
        # frames emitted. A keepalive gap does not mean the turn is over —
        # only prompt_complete means that. Without this, reasoning gaps > 30s
        # cause collect_response to exit early, losing subsequent speech.
        # (Session 43 bug: 784 chars of speech lost to keepalive timeout.)
        time_since_last_frame = time.monotonic() - last_frame_time
        if saw_prompt_complete and not pending_tool_calls:
            # Turn is ending — use tightened keepalive to catch response frame
            effective_keepalive = keepalive_timeout
        else:
            # Turn in progress — only respect wall clock
            effective_keepalive = max_wall_clock
        remaining_keepalive = effective_keepalive - time_since_last_frame
        remaining_wall = wall_deadline - time.monotonic()
        wait_timeout = max(0.1, min(remaining_keepalive, remaining_wall))

        if remaining_keepalive <= 0 or remaining_wall <= 0:
            break

        try:
            frame = await asyncio.wait_for(
                read_frame(stdout),
                timeout=wait_timeout
            )
        except asyncio.TimeoutError:
            break
        if frame is None:
            if saw_prompt_complete:
                # After prompt_complete, EOF means the response frame
                # didn't arrive. Not fatal — we have the speech already.
                break
            raise RuntimeError("stdio process closed stdout")

        # Frame received — reset keepalive timer
        last_frame_time = time.monotonic()

        method = frame.get("method", "")
        params = frame.get("params", {})
        update = params.get("update", {})

        if DEBUG:
            c = update.get('content', {})
            t = c.get('text', '') if isinstance(c, dict) else str(type(c).__name__)
            print(f"[debug] {method} {update.get('sessionUpdate','')} {str(t)[:60]}")

        # Agent speech chunk
        if method == "session/update" and update.get("sessionUpdate") == "agent_message_chunk":
            c = update.get("content", {})
            text = c.get("text", "") if isinstance(c, dict) else ""
            if text:
                if not first_chunk_marked and timer:
                    timer.mark("first_chunk")
                    first_chunk_marked = True
                speech_chunks.append(text)
                if on_speech_chunk:
                    on_speech_chunk(text)

        # Agent thought chunk
        elif method == "session/update" and update.get("sessionUpdate") == "agent_thought_chunk":
            c = update.get("content", {})
            text = c.get("text", "") if isinstance(c, dict) else ""
            if text:
                if not first_chunk_marked and timer:
                    timer.mark("first_chunk")
                    first_chunk_marked = True
                thought_chunks.append(text)

        # Tool call — track as pending and notify caller
        elif method == "session/update" and update.get("sessionUpdate") == "tool_call":
            tool_id = update.get("toolCallId")
            if tool_id:
                pending_tool_calls.add(tool_id)
            if on_tool_call:
                on_tool_call(update.get("title", ""))

        # Tool call update — remove from pending when completed
        elif method == "session/update" and update.get("sessionUpdate") == "tool_call_update":
            tool_id = update.get("toolCallId")
            if tool_id and update.get("status") == "completed":
                pending_tool_calls.discard(tool_id)

        # Extract metadata (totalTokens, modelId, etc.)
        # _meta is present on EVERY session/update frame (in params._meta)
        # AND on the final JSON-RPC response (in result._meta).
        # Extract from both — the streaming _meta gives us running token
        # counts even if we never see the final response frame (e.g., timeout).
        streaming_meta = params.get("_meta", {})
        if streaming_meta.get("totalTokens"):
            result_meta["totalTokens"] = streaming_meta["totalTokens"]
            if on_meta:
                on_meta(streaming_meta["totalTokens"])

        if "result" in frame:
            meta = frame.get("result", {}).get("_meta", {})
            if meta:
                result_meta = {
                    "totalTokens": meta.get("totalTokens", 0),
                    "modelId": meta.get("modelId", ""),
                    "stopReason": meta.get("stopReason", ""),
                }

        # Done — the JSON-RPC response (with id + result._meta) arrives
        # AFTER _x.ai/session/prompt_complete. If we break on prompt_complete,
        # we miss the _meta containing totalTokens. So when we see prompt_complete,
        # tighten the deadline to catch the response frame that follows.
        if frame.get("id") == prompt_id:
            break
        if method == "_x.ai/session/prompt_complete":
            # Response frame with _meta follows shortly — tighten keepalive to 2s
            # Only tighten if no tool calls are still pending (safety net —
            # prompt_complete should always come after all tools finish)
            saw_prompt_complete = True
            if not pending_tool_calls:
                keepalive_timeout = min(keepalive_timeout, 2.0)
            last_frame_time = time.monotonic()  # reset so the 2s starts now

    return "".join(speech_chunks), "".join(thought_chunks), result_meta


async def drain_stale_frames(stdout, agent_name=None):
    """Drain any buffered frames from stdout without blocking.
    
    After auto-compaction or long tool-call responses that exceed the
    collect_response timeout, stale frames may be sitting in the pipe.
    If not drained, they contaminate the next collect_response call,
    causing a one-behind desync where the response to prompt N is actually
    the response to prompt N-1.
    
    Collects any speech chunks found and delivers them via the outbox
    rather than silently discarding them. Non-speech frames (tool_call,
    prompt_complete, notifications) are logged and discarded.
    
    Call this before sending each new prompt to ensure a clean pipe.
    
    Returns (drained_count, speech_text) — speech_text is the recovered
    speech if any, or empty string.
    """
    drained = 0
    speech_chunks = []
    frame_types = []
    
    while True:
        try:
            frame = await asyncio.wait_for(read_frame(stdout), timeout=0.05)
            if frame is None:
                break
            method = frame.get("method", "")
            params = frame.get("params", {})
            update = params.get("update", {})
            utype = update.get("sessionUpdate", "")
            
            # Log every drained frame type for diagnostics
            if "result" in frame:
                frame_types.append("jsonrpc_response")
                # Extract _meta if present (safety net for token tracking)
                meta = frame.get("result", {}).get("_meta", {})
                if meta.get("totalTokens"):
                    print(f"[asdaaas] DRAIN: WARNING — drained response frame had totalTokens={meta['totalTokens']}. "
                          f"This means collect_response missed it.")
            else:
                frame_types.append(utype or method or "unknown")
            
            if utype == "agent_message_chunk":
                c = update.get("content", {})
                t = c.get("text", "") if isinstance(c, dict) else ""
                if t:
                    speech_chunks.append(t)
            elif utype == "agent_thought_chunk":
                pass  # discard stale thoughts
            elif method == "_x.ai/session/prompt_complete":
                pass  # expected terminator for the stale response
            # All other frame types (tool_call, notifications, etc.) are discarded
            
            drained += 1
        except asyncio.TimeoutError:
            break
    
    speech = "".join(speech_chunks).strip()
    
    if drained:
        # Log frame types for compaction/pipe diagnostics
        from collections import Counter
        type_counts = dict(Counter(frame_types))
        print(f"[asdaaas] DRAIN: {drained} stale frame(s), types: {type_counts}")
        if speech:
            # Log but DO NOT deliver. With the collect_response tool_call
            # tracking fix (Session 43), stale frames should be rare —
            # collect_response now extends its keepalive while tool calls are
            # in flight, preventing premature exit during long tool executions.
            #
            # If we still see stale speech here, it means either:
            # 1. A tool call exceeded max_wall_clock (600s) — extremely long
            # 2. A protocol anomaly (extra prompt_complete, orphaned frames)
            # 3. Post-compaction pipe desync
            #
            # In all cases, delivering stale speech would replay old responses
            # to the operator (Eric's "bunch of messages" bug, Session 43).
            # Log for diagnostics, discard for safety.
            print(f"[asdaaas] DRAIN: discarding {len(speech)} chars of stale speech: {speech[:80]}")
    
    return drained, speech


# ============================================================================
# GRACEFUL SHUTDOWN
# ============================================================================

def _request_shutdown(sig, agent_name):
    """Signal handler: set shutdown flag. Current turn finishes, then exit."""
    global _shutdown_requested
    sig_name = signal.Signals(sig).name
    print(f"\n[asdaaas] {sig_name} received for {agent_name} -- finishing current turn, then shutting down")
    _shutdown_requested = True


def request_shutdown_from_command(agent_name):
    """Command handler: same as signal, but triggered by commands.json."""
    global _shutdown_requested
    print(f"[asdaaas] Shutdown command received for {agent_name} -- finishing current turn, then shutting down")
    _shutdown_requested = True


# ============================================================================
# MAIN LOOP
# ============================================================================

async def main(agent_name, session_id=None, agent_cwd=None, model=None, backend=None):
    global _shutdown_requested

    # Register signal handlers for graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown, sig, agent_name)

    if agent_cwd is None:
        agent_cwd = str(config.agent_home(agent_name))

    # Create per-agent directory structure
    a_dir = agent_dir(agent_name)
    a_dir.mkdir(parents=True, exist_ok=True)
    (a_dir / "doorbells").mkdir(parents=True, exist_ok=True)
    (a_dir / "attention").mkdir(parents=True, exist_ok=True)
    (a_dir / "profile").mkdir(parents=True, exist_ok=True)
    (a_dir / "adapters").mkdir(parents=True, exist_ok=True)
    ADAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    
    # Auto-generate starter config files if they don't exist
    awareness_file = a_dir / "awareness.json"
    if not awareness_file.exists():
        starter_awareness = {
            "direct_attach": ["tui", "irc"],
            "control_watch": {},
            "notify_watch": [],
            "accept_from": ["*"],
            "default_doorbell": True,
            "doorbell_ttl": {"context": 1, "session": 2, "default": 3},
        }
        with open(awareness_file, "w") as f:
            json.dump(starter_awareness, f, indent=2)
        print(f"[asdaaas] Created starter awareness.json for {agent_name}")

    gaze_file = a_dir / "gaze.json"
    if not gaze_file.exists():
        starter_gaze = {
            "speech": {"target": "tui", "params": {}},
            "thoughts": None,
        }
        with open(gaze_file, "w") as f:
            json.dump(starter_gaze, f, indent=2)
        print(f"[asdaaas] Created starter gaze.json for {agent_name}")

    commands_dir = a_dir / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)

    # Register in running_agents.json so adapters can find us
    _register_running_agent(agent_name, agent_cwd)

    # Capture code version at startup (cached for lifetime of process)
    version = get_code_version()
    print(f"[asdaaas] ASDAAAS v2 starting for {agent_name} (code: {version})")
    _log_startup_event(agent_name, "init", "ok", f"code={version}")

    # ---- Create backend if not provided ----
    if backend is None:
        backend_type = config.agent_backend(agent_name)
        if backend_type == "claude":
            from claude_backend import ClaudeBackend
            backend = ClaudeBackend()
            print(f"[asdaaas] Backend: claude")
        else:
            grok_bin = os.environ.get("ASDAAAS_GROK_BINARY")
            try:
                kwargs = {"grok_sessions_dir": config.grok_sessions_dir}
                if grok_bin:
                    kwargs["grok_binary"] = grok_bin
                backend = GrokBackend(**kwargs)
            except Exception:
                backend = GrokBackend()
            print(f"[asdaaas] Backend: grok")

    # ---- Permission config ----
    agent_yolo = config.agent_yolo(agent_name)
    agent_mentor = config.agent_mentor(agent_name)
    if not agent_yolo and agent_mentor:
        print(f"[asdaaas] Yolo OFF — mentor: {agent_mentor}")

        async def _permission_handler(params):
            """Route permission request to mentor via file + localmail."""
            tool_call = params.get("toolCall", {})
            kind = tool_call.get("kind", "unknown")
            title = tool_call.get("title", "")
            print(f"[asdaaas] Permission request: {kind} — {title}")

            # Write pending request file
            req_id = write_permission_request(agent_name, params)
            print(f"[asdaaas] Wrote permission request: {req_id}")

            # Notify mentor via localmail
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                from localmail import send_mail
                send_mail(
                    from_agent=agent_name,
                    to_agent=agent_mentor,
                    text=(f"PERMISSION REQUEST from {agent_name}\n"
                          f"req_id: {req_id}\n"
                          f"kind: {kind}\n"
                          f"title: {title}\n\n"
                          f"To approve: approve_permission('{agent_name}', '{req_id}', decided_by='{agent_mentor}')\n"
                          f"To reject: reject_permission('{agent_name}', '{req_id}', decided_by='{agent_mentor}')\n"
                          f"To allow all {kind}: approve_permission('{agent_name}', '{req_id}', kind='allow-always', decided_by='{agent_mentor}')")
                )
            except Exception as e:
                print(f"[asdaaas] Warning: failed to notify mentor {agent_mentor}: {e}")

            # Poll for decision (0.5s interval, 5 min timeout)
            deadline = time.time() + 300
            while time.time() < deadline:
                decision = read_decision(agent_name, req_id)
                if decision:
                    option_id = decision.get("decision", "reject-once")
                    print(f"[asdaaas] Permission decided: {option_id} by {decision.get('decided_by')}")
                    archive_request(agent_name, req_id)
                    return option_id
                await asyncio.sleep(0.5)

            # Timeout — auto-reject
            print(f"[asdaaas] Permission timeout for {req_id} — rejecting")
            archive_request(agent_name, req_id)
            return "reject-once"

        backend.set_permission_handler(_permission_handler)

        # Pre-seed allowed kinds from config (skip mentor for these)
        allow_kinds = config.agent_allow_kinds(agent_name)
        if allow_kinds:
            backend._allowed_always.update(allow_kinds)
            print(f"[asdaaas] Pre-approved kinds: {allow_kinds}")

    elif not agent_yolo:
        print(f"[asdaaas] WARNING: Yolo OFF but no mentor configured — all tools will be rejected")

    # ---- Per-agent context window ----
    agent_ctx = config.agent_context_window(agent_name)
    if agent_ctx:
        backend.context_window = agent_ctx
        print(f"[asdaaas] Context window override: {agent_ctx}")

    # ---- Sandbox and permission rules ----
    agent_sandbox = config.agent_sandbox(agent_name)
    agent_allow_rules = config.agent_allow_rules(agent_name)
    agent_deny_rules = config.agent_deny_rules(agent_name)
    agent_permission_mode = config.agent_permission_mode(agent_name)
    if agent_sandbox:
        print(f"[asdaaas] Sandbox: {agent_sandbox}")
    if agent_allow_rules or agent_deny_rules:
        print(f"[asdaaas] Allow rules: {agent_allow_rules}, Deny rules: {agent_deny_rules}")
    if agent_permission_mode:
        print(f"[asdaaas] Permission mode: {agent_permission_mode}")
    agent_pid_namespace = config.agent_pid_namespace(agent_name)
    if agent_pid_namespace:
        print(f"[asdaaas] PID namespace isolation: enabled")

    # ---- Reasoning effort ----
    agent_reasoning_effort = os.environ.get("ASDAAAS_REASONING_EFFORT") or config.agent_reasoning_effort(agent_name)
    if agent_reasoning_effort:
        print(f"[asdaaas] Reasoning effort: {agent_reasoning_effort}")

    # ---- Start backend (spawn, init, session, model, yolo) ----
    print(f"[asdaaas] Starting backend: {type(backend).__name__}")
    _log_startup_event(agent_name, "backend_start", "ok", type(backend).__name__)
    try:
        interjection_enabled = config.agent_interjection_enabled(agent_name) if config else False
        sid = await backend.start(agent_cwd, model=model, session_id=session_id, yolo=agent_yolo,
                                  sandbox=agent_sandbox, allow_rules=agent_allow_rules,
                                  deny_rules=agent_deny_rules, permission_mode=agent_permission_mode,
                                  reasoning_effort=agent_reasoning_effort,
                                  interjection_enabled=interjection_enabled,
                                  agent_name=agent_name,
                                  pid_namespace=agent_pid_namespace)
    except Exception as e:
        _log_startup_event(agent_name, "backend_start", "fail", str(e)[:200])
        raise
    print(f"[asdaaas] PID {backend.proc.pid if backend.proc else '?'}")
    print(f"[asdaaas] Session: {sid}")
    _log_startup_event(agent_name, "session_loaded", "ok", f"sid={sid[:12]}")

    total_tokens = backend.total_tokens
    context_window = backend.context_window
    last_response_ts = None  # epoch timestamp of agent's last response completion
    last_was_foreground = True  # was the most recent agent activity a foreground (in-room) message?

    # Initialize conversation.jsonl seq counter from existing file
    global _conv_seq
    conv_path = agent_dir(agent_name) / "conversation.jsonl"
    if conv_path.exists():
        with open(conv_path) as f:
            _conv_seq = sum(1 for _ in f)
        print(f"[asdaaas] Conversation log: {_conv_seq} existing entries")
    else:
        _conv_seq = 0
        print(f"[asdaaas] Conversation log: new file")

    # Expose model/session/backend to module-level for health writes
    global _current_model_id, _current_session_id, _current_backend_type
    _current_model_id = backend.model_id
    _current_session_id = sid
    _current_backend_type = config.agent_backend(agent_name) if config else "grok"
    print(f"[asdaaas] Model: {_current_model_id}")

    # Throttled callback for real-time health updates during long responses.
    _last_health_write = 0

    def _on_streaming_meta(tokens):
        nonlocal total_tokens, _last_health_write
        total_tokens = tokens
        now = time.monotonic()
        if now - _last_health_write >= 2.0:
            write_health(agent_name, "working", f"streaming ({tokens} tokens)", tokens, context_window)
            _last_health_write = now

    # ---- Cancel turn support ----
    # cancel_event is set by the background watcher when cancel_turn.flag appears.
    # It's passed to collect_response which raises TurnCancelled.
    cancel_event = asyncio.Event()
    cancel_watcher_task = None

    print("[asdaaas] Ready.")
    _log_startup_event(agent_name, "ready", "ok", f"model={_current_model_id}")

    write_health(agent_name, "ready", f"session={sid}", total_tokens, context_window)

    # Update session registry so dashboards/health_check see current session
    _reg_path = os.path.expanduser("~/.grok/session_registry.json")
    try:
        _reg = {}
        if os.path.exists(_reg_path):
            with open(_reg_path) as _f:
                _reg = json.load(_f)
        _reg[agent_name] = {"session_id": sid, "status": "active"}
        with open(_reg_path, "w") as _f:
            json.dump(_reg, _f, indent=2)
        print(f"[asdaaas] Session registry updated: {agent_name} -> {sid[:8]}...")
    except Exception as _e:
        print(f"[asdaaas] WARN: failed to update session registry: {_e}")

    # ---- Observer (in-process async task, Phase 1 refactor) ----
    from binary_state_observer import InProcessObserver
    observer_state_file = str(config.agent_observer_state_file(agent_name)) if config else None
    in_process_observer = None

    if backend.proc and backend.session_dir:
        try:
            observer_state_file = str(config.agent_observer_state_file(agent_name))
            in_process_observer = InProcessObserver(
                pid=backend.proc.pid,
                session_dir=str(backend.session_dir),
                state_file=observer_state_file,
            )
            in_process_observer.start()
            backend.set_observer(in_process_observer)
            print(f"[asdaaas] Observer started (in-process, watching PID {backend.proc.pid})")
        except Exception as e:
            print(f"[asdaaas] WARN: Failed to start observer: {e}")
            in_process_observer = None
    else:
        print(f"[asdaaas] WARN: Backend not ready for observer (no proc or session_dir)")

    def read_observer_state():
        """Read observer state directly from in-process observer."""
        if in_process_observer:
            return in_process_observer.state_dict()
        return None

    # Read awareness file — determines which adapter inboxes to watch
    awareness = read_awareness(agent_name)
    print(f"[asdaaas] Awareness: direct={awareness.get('direct_attach', [])}, notify={awareness.get('notify_watch', [])}")
    print(f"[asdaaas] Polling for '{agent_name}'...")
    
    # Phase 4.4: Initialize command watchdog
    watchdog = CommandWatchdog(agent_name)
    
    # Pending message queue for background channels in "pending" mode
    pending_queue = PendingQueue()

    # S4: Turn engine — phases extracted from main() for testability
    env = AsdaaasEnv.from_config()
    engine = TurnEngine(env, agent_name, backend,
                        context_window=context_window,
                        watchdog=watchdog, pending_queue=pending_queue,
                        observer_state_file=observer_state_file)

    # Phase 7.2: Read adapter registrations
    adapters = read_adapter_registrations()
    print(f"[asdaaas] Adapters: {list(adapters.keys()) if adapters else '(none registered)'}")
    
    errors = 0

    # ---- Compaction state ----
    turns_since_compaction = COMPACTION_COOLDOWN_TURNS  # start as "available" (not just-compacted)
    compact_pending = None  # legacy: kept for auto-compaction cancellation only
    compact_pending_turns = 0
    _prev_tokens = total_tokens  # for detecting auto-compaction (token drop)
    next_turn_delay = 0  # seconds to wait before next default doorbell (0=immediate)
    delay_until_event = False  # if True, skip default doorbell entirely (wait for external)
    delay_text = None           # optional text payload from delay command, delivered on next continue
    did_work_this_iteration = False  # track if any work was done this loop iteration

    # ---- Reasoning effort state ----
    REASONING_EFFORT_TURN_LIMIT = 5
    reasoning_effort_turns_remaining = None  # None = no elevated level active
    reasoning_effort_default = agent_reasoning_effort  # configured default from agents.json
    consecutive_empty_doorbell = 0  # count consecutive empty doorbell responses (for backoff)
    last_delivered_bell_ids = set()  # bells delivered on previous iteration — skip on next poll (issue_0039)

    # ---- Main loop ----
    while True:
        try:
            # ---- Graceful shutdown check ----
            if _shutdown_requested:
                print(f"[asdaaas] Shutting down {agent_name} gracefully")
                write_health(agent_name, "shutdown", "graceful shutdown", total_tokens, context_window)
                break

            # ---- 0. Refresh token count from backend's authoritative source ----
            # Between turns the binary may have compacted or updated tokens.
            # refresh_tokens() also detects auto_compact_completed events
            # from updates.jsonl and updates total_tokens from tokens_after.
            total_tokens = backend.refresh_tokens()

            # ---- 0b. Detect compaction (event-based, observer-only) ----
            # Extracted to TurnEngine.handle_compaction_detection() (S4)
            engine.total_tokens = total_tokens
            engine.interjection_enabled = interjection_enabled
            compacted = await engine.handle_compaction_detection(
                on_streaming_meta=_on_streaming_meta)
            if compacted:
                total_tokens = engine.total_tokens
                turns_since_compaction = engine.turns_since_compaction
                _prev_tokens = engine._prev_tokens
                compact_pending = engine.compact_pending
                compact_pending_turns = engine.compact_pending_turns
                gaze = engine.gaze
                continue
            _prev_tokens = engine._prev_tokens

            # ---- 1. Check for adapter commands (e.g., /compact) ----
            commands = poll_commands(agent_name)
            for cmd in commands:
                action = cmd.get("action", "")
                request_id = cmd.get("request_id", "")
                print(f"[asdaaas] Command: {action} (req={request_id})")

                # ---- Piggyback ack: any command can carry an "ack" field ----
                # Solves the single-slot race: agent writes one command file
                # with both the action and ack ids, both processed atomically.
                # E.g.: {"action": "delay", "seconds": 300, "ack": ["bell_001"]}
                piggyback_ack = cmd.get("ack", [])
                if piggyback_ack:
                    removed = ack_doorbells(agent_name, piggyback_ack)
                    print(f"[asdaaas] Piggyback ack: {removed} doorbell(s) cleared")

                if action == "delay":
                    delay_val = cmd.get("seconds", 0)
                    delay_text = cmd.get("text") or None  # optional directed text for next continue
                    if delay_val == "until_event":
                        delay_until_event = True
                        next_turn_delay = 0
                        # Clear any existing continue doorbells -- the agent
                        # is saying "don't wake me."  Without this, persistent
                        # continue bells keep re-delivering every iteration.
                        _cleanup_continue_doorbells(agent_name)
                        print(f"[asdaaas] Delay: until_event (standing by)")
                    else:
                        next_turn_delay = float(delay_val)
                        delay_until_event = False
                        # Clean up stale continue doorbells — without this,
                        # a continue bell from the previous iteration races
                        # past the delay command and wakes the agent immediately.
                        _cleanup_continue_doorbells(agent_name)
                        msg = f"[asdaaas] Delay: {next_turn_delay}s before next default doorbell"
                        if delay_text:
                            msg += f" (text: {delay_text[:60]})"
                        print(msg)

                elif action == "ack":
                    handled = cmd.get("handled", [])
                    if handled:
                        removed = ack_doorbells(agent_name, handled)
                        print(f"[asdaaas] Ack: {removed} doorbell(s) cleared")

                elif action == "compact":
                    # Extracted to TurnEngine.handle_compact_command() (S4)
                    engine.turns_since_compaction = turns_since_compaction
                    engine.compact_pending = compact_pending
                    engine.compact_pending_turns = compact_pending_turns
                    engine.total_tokens = total_tokens
                    await engine.handle_compact_command(cmd, on_streaming_meta=_on_streaming_meta)
                    total_tokens = engine.total_tokens
                    turns_since_compaction = engine.turns_since_compaction
                    _prev_tokens = engine._prev_tokens
                    compact_pending = engine.compact_pending
                    compact_pending_turns = engine.compact_pending_turns
                    gaze = engine.gaze

                elif action == "force_compact":
                    # Extracted to TurnEngine.handle_force_compact_command() (S4)
                    engine.turns_since_compaction = turns_since_compaction
                    engine.compact_pending = compact_pending
                    engine.compact_pending_turns = compact_pending_turns
                    engine.total_tokens = total_tokens
                    await engine.handle_force_compact_command(cmd, on_streaming_meta=_on_streaming_meta)
                    total_tokens = engine.total_tokens
                    turns_since_compaction = engine.turns_since_compaction
                    _prev_tokens = engine._prev_tokens
                    compact_pending = engine.compact_pending
                    compact_pending_turns = engine.compact_pending_turns
                    gaze = engine.gaze

                elif action == "interrupt":
                    # External operator tool: inject a high-priority message into the agent's next prompt.
                    # Used when agent is stuck/looping and operator needs to break in.
                    interrupt_text = cmd.get("text", "Operator interrupt: please acknowledge and report status.")
                    bell_dir = agent_dir(agent_name) / "doorbells"
                    bell_dir.mkdir(parents=True, exist_ok=True)
                    bell = {
                        "adapter": "operator",
                        "command": "interrupt",
                        "priority": 0,  # highest priority -- delivered first
                        "text": f"[OPERATOR INTERRUPT] {interrupt_text}",
                        "ts": time.time(),
                    }
                    fd, tmp_path = tempfile.mkstemp(dir=str(bell_dir), suffix=".tmp", prefix="int_")
                    with os.fdopen(fd, "w") as f:
                        json.dump(bell, f)
                    os.rename(tmp_path, tmp_path.replace(".tmp", ".json"))
                    # Also cancel any delay -- agent should wake up immediately
                    next_turn_delay = 0
                    delay_until_event = False
                    print(f"[asdaaas] Operator interrupt delivered to {agent_name}")

                elif action == "shutdown":
                    request_shutdown_from_command(agent_name)
                    # Flag is set; loop will break at top of next iteration
                    # (current turn is already between turns, so exit is immediate)

                elif action == "gaze":
                    # Set gaze target. Validates and writes gaze.json.
                    # Usage: {"action": "gaze", "adapter": "irc", "room": "#meetingroom1"}
                    #        {"action": "gaze", "adapter": "irc", "pm": "eric"}
                    #        {"action": "gaze", "off": true}  -- clear gaze
                    new_gaze = _build_gaze(cmd)
                    if new_gaze is not None:
                        write_gaze(agent_name, new_gaze)
                        _, room = get_room(new_gaze)
                        print(f"[asdaaas] GAZE: {agent_name} -> {cmd.get('adapter', 'off')}:{room or 'none'}")
                    else:
                        print(f"[asdaaas] GAZE: invalid command: {cmd}")

                elif action == "reasoning_effort":
                    # Change reasoning effort level mid-session via session/set_model.
                    # Usage: {"action": "reasoning_effort", "level": "high"}
                    # Levels: low, medium, high
                    # Renewal: if already at requested level, just resets the turn counter.
                    new_level = cmd.get("level", "")
                    turns = cmd.get("turns", REASONING_EFFORT_TURN_LIMIT)
                    valid_levels = ("low", "medium", "high")
                    if new_level in valid_levels:
                        current_level = backend._start_kwargs.get("reasoning_effort") or reasoning_effort_default
                        if current_level == new_level:
                            # Renewal — same level, just reset counter
                            reasoning_effort_turns_remaining = int(turns)
                            print(f"[asdaaas] REASONING EFFORT: renewed {new_level} for {turns} turns")
                        else:
                            # Level change — session/set_model with _meta.reasoningEffort
                            old_level = current_level or "default"
                            print(f"[asdaaas] REASONING EFFORT: {old_level} -> {new_level}...")
                            try:
                                await backend.set_reasoning_effort(new_level)
                                backend._start_kwargs["reasoning_effort"] = new_level
                                reasoning_effort_turns_remaining = int(turns)
                                print(f"[asdaaas] REASONING EFFORT: set to {new_level} for {turns} turns")
                            except Exception as e:
                                print(f"[asdaaas] ERROR setting reasoning effort: {e}")
                    else:
                        print(f"[asdaaas] REASONING EFFORT: invalid level '{new_level}' (valid: {valid_levels})")

                elif action == "awareness":
                    # Modify awareness config. Reads current, applies change, writes back.
                    # Usage: {"action": "awareness", "add": "#meetingroom1", "mode": "doorbell"}
                    #        {"action": "awareness", "remove": "#meetingroom1"}
                    #        {"action": "awareness", "default": "pending"}
                    #        {"action": "awareness", "doorbell_ttl": {"irc": 3}}
                    current = read_awareness(agent_name)
                    updated, desc = _apply_awareness_command(cmd, current)
                    if updated is not None:
                        write_awareness(agent_name, updated)
                        awareness = updated  # refresh local copy
                        print(f"[asdaaas] AWARENESS: {agent_name} -- {desc}")
                    else:
                        print(f"[asdaaas] AWARENESS: error for {agent_name} -- {desc}")

            # ---- 1b. Start cancel watcher for this iteration ----
            # Watches for cancel_turn.flag during collect_response calls.
            # Started fresh each iteration, cancelled at end.
            cancel_event.clear()
            if cancel_watcher_task and not cancel_watcher_task.done():
                cancel_watcher_task.cancel()
                try:
                    await cancel_watcher_task
                except asyncio.CancelledError:
                    pass
            cancel_watcher_task = asyncio.create_task(
                watch_cancel_flag(agent_name, cancel_event))

            # ---- 1c. Check watchdog timeouts (Phase 4.4) ----
            watchdog.deliver_timeout_doorbells(agent_name)

            # ---- 1c. Re-read adapter registrations periodically (Phase 7.2) ----
            adapters = read_adapter_registrations()

            # ---- 2. Gather ALL pending items before sending any prompt ----
            # Extracted to TurnEngine.gather_pending() (S4 decomposition)
            gathered = await engine.gather_pending()
            bells = gathered.doorbells
            in_room_msgs = gathered.messages
            bg_doorbell_msgs = gathered.bg_doorbells
            messages = bells + in_room_msgs + bg_doorbell_msgs  # for idle check below
            awareness = engine.awareness
            gaze = engine.gaze
            last_delivered_bell_ids = engine.last_delivered_bell_ids
            did_work_this_iteration = False

            # ---- 3. Nothing pending? Handle idle / default doorbell ----
            # Extracted to TurnEngine.handle_idle() (S4)
            if not messages and not bells and not commands:
                engine.next_turn_delay = next_turn_delay
                engine.delay_until_event = delay_until_event
                engine.delay_text = delay_text
                engine.total_tokens = total_tokens
                idle_result = await engine.handle_idle()
                next_turn_delay = engine.next_turn_delay
                delay_until_event = engine.delay_until_event
                delay_text = engine.delay_text
                last_was_foreground = engine.last_was_foreground
                if idle_result.action == "sleep":
                    await asyncio.sleep(IDLE_POLL_INTERVAL)
                continue

            # ==== 4. COALESCED DELIVERY: doorbells + in-room messages ====
            # Set reasoning effort info for context tag
            if reasoning_effort_turns_remaining is not None:
                current_re = backend._start_kwargs.get("reasoning_effort") or "default"
                engine.reasoning_effort_info = (current_re, reasoning_effort_turns_remaining)
            else:
                engine.reasoning_effort_info = None

            # Extracted to TurnEngine.deliver_turn() (S4 decomposition)
            deliver_result = await engine.deliver_turn(
                gathered,
                cancel_event=cancel_event,
                interjection_enabled=interjection_enabled,
                on_streaming_meta=_on_streaming_meta)

            if deliver_result is not None:
                did_work_this_iteration = True
                result = deliver_result
                total_tokens = engine.total_tokens
                turns_since_compaction = engine.turns_since_compaction
                gaze = engine.gaze
                # Unpack delivery metadata for post-turn processing
                timer = deliver_result._timer
                has_bells = deliver_result._has_bells
                has_msgs = deliver_result._has_msgs
                # Track which bells were just delivered (issue_0039)
                if bells:
                    last_delivered_bell_ids = {b.get("id") for b in bells if b.get("id")}
                    engine.last_delivered_bell_ids = last_delivered_bell_ids
                # ---- Post-turn processing ----
                # Extracted to TurnEngine.post_turn() (S4 decomposition)
                engine.interjection_enabled = interjection_enabled
                post_result = await engine.post_turn(deliver_result)
                # Sync engine state back to main() locals
                next_turn_delay = engine.next_turn_delay
                delay_until_event = engine.delay_until_event
                delay_text = engine.delay_text
                last_response_ts = engine.last_response_ts
                last_was_foreground = engine.last_was_foreground
                consecutive_empty_doorbell = engine.consecutive_empty_doorbell
                gaze = engine.gaze
                total_tokens = engine.total_tokens
                agent_wrote_delay = post_result.agent_wrote_delay

                # ---- Reasoning effort countdown ----
                if reasoning_effort_turns_remaining is not None:
                    reasoning_effort_turns_remaining -= 1
                    current_level = backend._start_kwargs.get("reasoning_effort") or "default"
                    if reasoning_effort_turns_remaining <= 0:
                        # Auto-revert to default via session/set_model
                        revert_to = reasoning_effort_default or "high"
                        if current_level != revert_to:
                            print(f"[asdaaas] REASONING EFFORT: expired, reverting {current_level} -> {revert_to}...")
                            try:
                                await backend.set_reasoning_effort(revert_to)
                                backend._start_kwargs["reasoning_effort"] = revert_to
                                print(f"[asdaaas] REASONING EFFORT: reverted to {revert_to}")
                            except Exception as e:
                                print(f"[asdaaas] ERROR reverting reasoning effort: {e}")
                        reasoning_effort_turns_remaining = None
                    else:
                        print(f"[asdaaas] REASONING EFFORT: {current_level}, {reasoning_effort_turns_remaining} turns remaining")

            # ==== 5. Background doorbell messages (separate delivery) ====
            # Extracted to TurnEngine.deliver_background_doorbells() (S4)
            if bg_doorbell_msgs:
                engine.total_tokens = total_tokens
                engine.turns_since_compaction = turns_since_compaction
                await engine.deliver_background_doorbells(
                    bg_doorbell_msgs, cancel_event=cancel_event,
                    on_streaming_meta=_on_streaming_meta)
                total_tokens = engine.total_tokens
                turns_since_compaction = engine.turns_since_compaction
                last_response_ts = engine.last_response_ts
                last_was_foreground = engine.last_was_foreground

            errors = 0

        except TurnCancelled:
            # Mid-turn cancel triggered via cancel_turn.flag
            print(f"[asdaaas] CANCEL: Turn cancelled for {agent_name}")
            
            # Clean up the flag file
            flag = cancel_turn_flag_path(agent_name)
            try:
                flag.unlink(missing_ok=True)
            except OSError:
                pass
            
            # Stop the cancel watcher if running
            if cancel_watcher_task and not cancel_watcher_task.done():
                cancel_watcher_task.cancel()
                try:
                    await cancel_watcher_task
                except asyncio.CancelledError:
                    pass
            cancel_watcher_task = None
            cancel_event.clear()
            
            # Kill and restart the backend
            print(f"[asdaaas] CANCEL: Killing and restarting backend...")
            write_health(agent_name, "restarting", "mid-turn cancel", total_tokens, context_window)
            try:
                new_sid = await backend.cancel_and_restart(agent_cwd)
                total_tokens = backend.total_tokens
                print(f"[asdaaas] CANCEL: Restarted. Session {new_sid}, {total_tokens} tokens")
                # Reset observer for new PID and session dir
                if in_process_observer and backend.proc:
                    in_process_observer.reset(backend.proc.pid, str(backend.session_dir))
                    print(f"[asdaaas] CANCEL: Observer reset for PID {backend.proc.pid}")
                write_health(agent_name, "active", f"cancelled and restarted", total_tokens, context_window)
                
                # Deliver a doorbell so the agent knows what happened
                bell_dir = agent_dir(agent_name) / "doorbells"
                bell_dir.mkdir(parents=True, exist_ok=True)
                bell = {
                    "adapter": "session",
                    "command": "cancel_turn",
                    "priority": 1,
                    "text": "[session:cancel_turn] Your previous turn was cancelled by the operator. You have been restarted with your session intact. The partial turn was discarded.",
                    "ts": time.time(),
                }
                fd, tmp_path = tempfile.mkstemp(dir=str(bell_dir), suffix=".tmp", prefix="cancel_")
                with os.fdopen(fd, "w") as f:
                    json.dump(bell, f)
                os.rename(tmp_path, tmp_path.replace(".tmp", ".json"))
                
                # Wake agent immediately
                next_turn_delay = 0
                delay_until_event = False
                
            except Exception as restart_err:
                print(f"[asdaaas] CANCEL: Restart failed: {restart_err}")
                import traceback
                traceback.print_exc()
                write_health(agent_name, "error", f"cancel restart failed: {restart_err}", total_tokens, context_window)
                errors += 1

        except Exception as e:
            errors += 1
            print(f"[asdaaas] Error #{errors}: {e}")
            import traceback
            traceback.print_exc()
            write_health(agent_name, "error", str(e)[:100], total_tokens, context_window)
            if errors > 10:
                print("[asdaaas] Too many errors, exiting")
                break
            await asyncio.sleep(2.0)

    # ---- Cleanup ----
    # Stop in-process observer
    if in_process_observer:
        in_process_observer.stop()
        print(f"[asdaaas] Observer stopped")

    # Unregister first -- if backend.shutdown() hangs and the process
    # gets killed, the agent should not appear as running.
    _unregister_running_agent(agent_name)
    print(f"[asdaaas] Stopping {type(backend).__name__} subprocess for {agent_name}...")
    await backend.shutdown()
    print(f"[asdaaas] {agent_name} shut down.")


def _unregister_running_agent(agent_name, env: Optional[AsdaaasEnv] = None):
    """Remove agent from running_agents.json on shutdown."""
    env = env or AsdaaasEnv.from_config()
    reg_path = env.asdaaas_dir / "running_agents.json"
    try:
        with open(reg_path) as f:
            reg = json.load(f)
        if agent_name in reg:
            del reg[agent_name]
            with open(reg_path, "w") as f:
                json.dump(reg, f, indent=2)
            print(f"[asdaaas] Unregistered {agent_name} from running_agents.json")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ASDAAAS v2")
    parser.add_argument("--agent", default="Test", help="Agent name")
    parser.add_argument("--cwd", default=str(config.agents_home.parent), help="Working directory for agent")
    parser.add_argument("--session", default=None, help="Session ID to load")
    parser.add_argument("--model", "-m", default=None, help="Model ID (e.g., coding-mix-latest)")
    parser.add_argument("--backend", default="grok", choices=["grok", "claude"],
                        help="Agent backend (default: grok)")
    parser.add_argument("--api-key", default=None,
                        help="API key for claude backend (or set ANTHROPIC_API_KEY)")
    parser.add_argument("--grok-binary", default=None,
                        help="Path to grok binary (default: 'grok' from PATH)")
    parser.add_argument("--reasoning-effort", default=None,
                        choices=["high", "medium", "low"],
                        help="Reasoning effort level (overrides agents.json)")
    args = parser.parse_args()

    # Create backend — let main() read from config unless CLI explicitly overrides
    backend_instance = None
    if args.backend == "claude":
        from claude_backend import ClaudeBackend
        backend_instance = ClaudeBackend(api_key=args.api_key)

    # Pass grok_binary so main() can use it when creating GrokBackend from config
    if not backend_instance and args.grok_binary:
        os.environ["ASDAAAS_GROK_BINARY"] = args.grok_binary

    # CLI reasoning-effort overrides agents.json config
    if args.reasoning_effort:
        os.environ["ASDAAAS_REASONING_EFFORT"] = args.reasoning_effort

    try:
        asyncio.run(main(args.agent, args.session, args.cwd, args.model, backend=backend_instance))
    except KeyboardInterrupt:
        print("\n[asdaaas] Shut down.")
