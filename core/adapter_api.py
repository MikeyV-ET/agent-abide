#!/usr/bin/env python3
"""
MikeyV Adapter API — Filesystem-based message passing for hub adapters.

Any adapter (IRC, Slack, voice, web) uses this library to:
  1. Write messages to the hub inbox (mkstemp + rename for atomicity)
  2. Poll its own outbox for responses from the hub

Directory structure:
  ~/asdaaas/inbox/          — adapters write here, hub reads + deletes
  ~/asdaaas/outbox/<name>/  — hub writes here, adapters read + delete

Message format (JSON):
  {
    "id":      "uuid",
    "from":    "eric",
    "to":      "Sr",           # agent name or "broadcast"
    "text":    "message body",
    "adapter": "irc",          # adapter name (matches outbox subdir)
    "meta":    {},              # adapter-specific metadata
    "ts":      "epoch float (time.time())"
  }

Collision safety:
  - mkstemp() gives kernel-guaranteed unique filenames
  - os.rename() is atomic on Linux
  - Hub only sees .json files (ignores .tmp during write)
  - Benchmarked: 109 microseconds per write+rename, 0 collisions in 1500-msg test
"""

import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

# ============================================================================
# PATHS
# ============================================================================

try:
    from asdaaas_config import config
except ModuleNotFoundError:
    import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'core'))
    from asdaaas_config import config

HUB_DIR = config.hub_dir
AGENTS_DIR = HUB_DIR / "agents"  # legacy
AGENTS_HOME_DIR = config.agents_home
INBOX_DIR = config.inbox_dir
OUTBOX_DIR = config.outbox_dir


def ensure_dirs(adapter_name: Optional[str] = None):
    """Create hub directories. Call once at adapter startup."""
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    if adapter_name:
        (OUTBOX_DIR / adapter_name).mkdir(parents=True, exist_ok=True)


# ============================================================================
# WRITE TO HUB (adapter -> hub)
# ============================================================================

def write_message(
    to: str,
    text: str,
    adapter: str,
    sender: str = None,
    meta: Optional[dict] = None,
    msg_id: Optional[str] = None,
    expect_response: bool = False,
    timeout: Optional[int] = None,
) -> str:
    """
    Write a message to the hub inbox. Atomic via mkstemp + rename.

    Args:
        to:      Target agent name ("Sr", "Jr", "Trip", "Q", "Cinco", or "broadcast")
        text:    Message body
        adapter: Adapter name (e.g. "irc", "slack", "voice")
        sender:  Who sent this (e.g. "eric", "MikeyV-Jr")
        meta:    Adapter-specific metadata (e.g. {"channel": "#standup"})
        msg_id:  Optional message ID. Auto-generated if not provided.

    Returns:
        The message ID.
    """
    ensure_dirs()

    # Default sender to adapter name (fixes "unknown" issue)
    if sender is None:
        sender = adapter

    msg_id = msg_id or str(uuid.uuid4())
    meta = meta or {}

    # Attention structure: callback registration
    if expect_response:
        meta["expect_response"] = True
    if timeout is not None:
        meta["timeout"] = timeout

    msg = {
        "id": msg_id,
        "from": sender,
        "to": to,
        "text": text,
        "adapter": adapter,
        "meta": meta,
        "ts": time.time(),
    }

    # Atomic write: mkstemp creates unique file, rename makes it visible
    # Timestamp prefix ensures sorted() produces arrival order
    ts_ms = int(time.time() * 1000)
    fd, tmp_path = tempfile.mkstemp(dir=str(INBOX_DIR), suffix=".tmp", prefix=f"msg_{ts_ms}_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(msg, f)
        # Rename .tmp -> .json (atomic on Linux)
        final_path = tmp_path.replace(".tmp", ".json")
        os.rename(tmp_path, final_path)
    except Exception:
        # Clean up on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return msg_id


# ============================================================================
# ATTENTION DECLARATIONS
# ============================================================================

ATTENTION_DIR = AGENTS_DIR  # legacy alias for test monkeypatching


def write_attention(
    agent_name: str,
    expecting_from: str,
    msg_id: str,
    timeout_s: int = 30,
    message_text: str = "",
) -> str:
    """
    Write an attention declaration. The agent is declaring: "I sent a message
    to expecting_from and I want to know when they respond, or if they don't
    respond within timeout_s seconds."

    asdaaas reads these files and enforces the boundaries -- delivering
    [RESPONSE] or [TIMEOUT] doorbells to the agent.

    Args:
        agent_name:     The agent creating the attention (e.g. "Jr")
        expecting_from: Who the agent is waiting for (e.g. "Trip")
        msg_id:         The message ID (links attention to the original message)
        timeout_s:      Seconds before timeout fires (default 30)
        message_text:   The original message text (for context in notifications)

    Returns:
        The msg_id.
    """
    attn_dir = AGENTS_HOME_DIR / agent_name / "asdaaas" / "attention"
    attn_dir.mkdir(parents=True, exist_ok=True)

    now = time.time()
    attn = {
        "msg_id": msg_id,
        "expecting_from": expecting_from,
        "timeout_s": timeout_s,
        "created_at": now,
        "expires_at": now + timeout_s,
        "message_text": message_text[:200],
        "status": "pending",
    }

    attn_file = attn_dir / f"{msg_id}.json"
    fd, tmp_path = tempfile.mkstemp(dir=str(attn_dir), suffix=".tmp", prefix="attn_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(attn, f)
        os.rename(tmp_path, str(attn_file))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return msg_id


def send_with_attention(
    to: str,
    text: str,
    adapter: str,
    sender: str = None,
    timeout: int = 30,
    meta: Optional[dict] = None,
) -> str:
    """
    Send a message AND create an attention declaration in one call.
    Convenience wrapper around write_message + write_attention.

    The agent says: "Send this to <to>, and tell me when they respond
    or if they don't respond within <timeout> seconds."

    Args:
        to:       Target agent name (e.g. "Trip", "Jr")
        text:     Message text
        adapter:  Sending adapter name (used as sender identity)
        sender:   Override sender name (defaults to adapter)
        timeout:  Seconds before timeout (default 30)
        meta:     Additional metadata

    Returns:
        The message ID.
    """
    sender = sender or adapter
    msg_id = str(uuid.uuid4())
    meta = meta or {}

    # Write the message to the hub inbox (universal delivery)
    write_message(
        to=to,
        text=text,
        adapter=adapter,
        sender=sender,
        meta=meta,
        msg_id=msg_id,
    )

    # Create the attention declaration for the sender
    # Agent name should be capitalized (e.g. "Sr", "Jr")
    agent_name = sender if sender[0].isupper() else sender.capitalize()
    write_attention(
        agent_name=agent_name,
        expecting_from=to,
        msg_id=msg_id,
        timeout_s=timeout,
        message_text=text,
    )

    return msg_id


# ============================================================================
# READ FROM HUB (hub -> adapter via outbox)
# ============================================================================

def poll_responses(adapter_name: str, delete: bool = True) -> list:
    """
    Poll the adapter's outbox for responses from the hub.

    Args:
        adapter_name: Adapter name (must match what was used in write_message)
        delete:       Delete files after reading (default True)

    Returns:
        List of response dicts, oldest first.
    """
    outbox = OUTBOX_DIR / adapter_name
    if not outbox.exists():
        return []

    responses = []
    # Sort by filename for chronological order
    for entry in sorted(outbox.iterdir()):
        if not entry.name.endswith(".json"):
            continue
        try:
            with open(entry, "r") as f:
                data = json.load(f)
            responses.append(data)
            if delete:
                entry.unlink()
        except json.JSONDecodeError:
            # Partial write or corruption — skip, will retry next poll
            pass
        except Exception as e:
            print(f"[adapter_api] Error reading {entry}: {e}")

    return responses


# ============================================================================
# RESPONSE WRITER (hub uses this to write to adapter outbox)
# ============================================================================

def write_response(
    adapter_name: str,
    request_id: str,
    from_agent: str,
    text: str,
    meta: Optional[dict] = None,
) -> str:
    """
    Write a response to an adapter's outbox. Called by the hub, not by adapters.

    Args:
        adapter_name: Which adapter's outbox to write to
        request_id:   The original message ID this is responding to
        from_agent:   Which agent responded
        text:         Response text
        meta:         Additional metadata

    Returns:
        The response ID.
    """
    ensure_dirs(adapter_name)

    resp_id = str(uuid.uuid4())
    resp = {
        "id": resp_id,
        "request_id": request_id,
        "from": from_agent,
        "text": text,
        "adapter": adapter_name,
        "meta": meta or {},
        "ts": time.time(),
    }

    outbox = OUTBOX_DIR / adapter_name
    fd, tmp_path = tempfile.mkstemp(dir=str(outbox), suffix=".tmp", prefix="resp_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(resp, f)
        final_path = tmp_path.replace(".tmp", ".json")
        os.rename(tmp_path, final_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return resp_id




# ============================================================================
# ADAPTER REGISTRATION (v2)
# ============================================================================
# Adapters register on startup by writing a JSON file to ~/asdaaas/adapters/
# Hub reads these to know what adapters are available, their capabilities,
# and whether they're still alive (heartbeat via mtime).
#
# Registration file: ~/asdaaas/adapters/<adapter_name>.json
# {
#   "name":         "irc",
#   "pid":          12345,
#   "started":      "2026-03-24T11:30:00",
#   "capabilities": ["send", "receive", "broadcast"],
#   "config":       {"channel": "#standup", "nick": "MikeyV-IRC"},
#   "heartbeat":    "2026-03-24T11:30:00"
# }

ADAPTERS_DIR = HUB_DIR / "adapters"


def register_adapter(
    name: str,
    capabilities: list = None,
    config: dict = None,
) -> Path:
    """
    Register an adapter with the hub. Call once at adapter startup.
    Updates heartbeat timestamp on each call, so can also be used as keepalive.

    Args:
        name:         Adapter name (e.g. "irc", "slack", "voice")
        capabilities: List of capabilities (e.g. ["send", "receive", "broadcast"])
        config:       Adapter-specific config to expose to hub

    Returns:
        Path to the registration file.
    """
    ADAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    ensure_dirs(name)

    reg = {
        "name": name,
        "pid": os.getpid(),
        "started": time.time(),
        "capabilities": capabilities or ["send", "receive"],
        "config": config or {},
        "heartbeat": time.time(),
    }

    reg_path = ADAPTERS_DIR / f"{name}.json"

    # If already registered (restart), preserve started time
    if reg_path.exists():
        try:
            with open(reg_path) as f:
                old = json.load(f)
            if old.get("pid") != os.getpid():
                # Different process — fresh registration
                pass
            else:
                # Same process — just update heartbeat
                reg["started"] = old.get("started", reg["started"])
        except (json.JSONDecodeError, KeyError):
            pass

    # Atomic write
    fd, tmp_path = tempfile.mkstemp(dir=str(ADAPTERS_DIR), suffix=".tmp", prefix="reg_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(reg, f, indent=2)
        os.rename(tmp_path, str(reg_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return reg_path


def update_heartbeat(name: str):
    """
    Update the heartbeat timestamp for a registered adapter.
    Call periodically (e.g. every 30s) to signal liveness.
    """
    reg_path = ADAPTERS_DIR / f"{name}.json"
    if not reg_path.exists():
        return

    try:
        with open(reg_path) as f:
            reg = json.load(f)
        reg["heartbeat"] = time.time()
        reg["pid"] = os.getpid()

        fd, tmp_path = tempfile.mkstemp(dir=str(ADAPTERS_DIR), suffix=".tmp", prefix="hb_")
        with os.fdopen(fd, "w") as f:
            json.dump(reg, f, indent=2)
        os.rename(tmp_path, str(reg_path))
    except Exception:
        pass


def deregister_adapter(name: str):
    """
    Remove adapter registration. Call on clean shutdown.
    """
    reg_path = ADAPTERS_DIR / f"{name}.json"
    try:
        reg_path.unlink(missing_ok=True)
    except Exception:
        pass


def list_adapters(max_heartbeat_age: int = 120) -> list:
    """
    List all registered adapters. Optionally filter by heartbeat freshness.

    Args:
        max_heartbeat_age: Max seconds since last heartbeat to consider "alive".
                          Set to 0 to return all regardless of freshness.

    Returns:
        List of adapter registration dicts, with added "alive" field.
    """
    if not ADAPTERS_DIR.exists():
        return []

    adapters = []
    now = time.time()

    for entry in sorted(ADAPTERS_DIR.iterdir()):
        if not entry.name.endswith(".json"):
            continue
        try:
            with open(entry) as f:
                reg = json.load(f)

            # Check liveness via file mtime (more reliable than parsed timestamp)
            mtime = entry.stat().st_mtime
            age = now - mtime
            reg["alive"] = (age <= max_heartbeat_age) if max_heartbeat_age > 0 else True
            reg["heartbeat_age_s"] = round(age, 1)

            # Also check if PID is still running
            pid = reg.get("pid")
            if pid:
                try:
                    os.kill(pid, 0)  # signal 0 = check existence
                    reg["process_alive"] = True
                except (OSError, ProcessLookupError):
                    reg["process_alive"] = False
                    reg["alive"] = False

            adapters.append(reg)
        except (json.JSONDecodeError, Exception):
            continue

    return adapters


def get_adapter(name: str) -> Optional[dict]:
    """Get registration info for a specific adapter."""
    reg_path = ADAPTERS_DIR / f"{name}.json"
    if not reg_path.exists():
        return None
    try:
        with open(reg_path) as f:
            return json.load(f)
    except Exception:
        return None


# ============================================================================
# STATUS QUERY API (v2)
# ============================================================================
# Adapters and agents can query the hub for system status by writing a
# query message to the inbox with to="hub" and meta.type="status_query".
#
# The hub responds with current status in the adapter's outbox.
#
# Query types:
#   "agents"   — list all agents with availability and session info
#   "adapters" — list all registered adapters with liveness
#   "health"   — overall system health summary
#   "all"      — everything above

STATUS_QUERY_DIR = HUB_DIR / "status"


def query_status(
    adapter_name: str,
    query_type: str = "all",
) -> str:
    """
    Send a status query to the hub. Response appears in adapter's outbox.

    Args:
        adapter_name: Your adapter name (response goes to your outbox)
        query_type:   "agents", "adapters", "health", or "all"

    Returns:
        Query message ID (poll your outbox for the response).
    """
    return write_message(
        to="hub",
        text=f"status_query:{query_type}",
        adapter=adapter_name,
        sender=adapter_name,
        meta={"type": "status_query", "query": query_type},
    )


def build_status_response(query_type: str = "all") -> dict:
    """
    Build a status response dict. Called by the hub when it receives a status query.

    Args:
        query_type: "agents", "adapters", "health", or "all"

    Returns:
        Dict with requested status information.
    """
    result = {"query": query_type, "ts": time.time()}

    if query_type in ("agents", "all"):
        # Read session registry
        reg_path = os.path.expanduser("~/.grok/session_registry.json")
        try:
            with open(reg_path) as f:
                registry = json.load(f)
            agents = {}
            for name, info in registry.items():
                agents[name] = {
                    "status": info.get("status", "unknown"),
                    "session_id": info.get("session_id", "unknown")[:12],
                }
            result["agents"] = agents
        except Exception as e:
            result["agents"] = {"error": str(e)}

    if query_type in ("adapters", "all"):
        result["adapters"] = list_adapters(max_heartbeat_age=120)

    if query_type in ("health", "all"):
        # Queue depths
        queues = {}
        if INBOX_DIR.exists():
            queues["inbox_pending"] = len(list(INBOX_DIR.glob("*.json")))
        if OUTBOX_DIR.exists():
            for d in OUTBOX_DIR.iterdir():
                if d.is_dir():
                    queues[f"outbox_{d.name}"] = len(list(d.glob("*.json")))
        result["queues"] = queues

        # Adapter count
        all_adapters = list_adapters(max_heartbeat_age=0)
        alive_adapters = [a for a in all_adapters if a.get("alive")]
        result["health"] = {
            "adapters_total": len(all_adapters),
            "adapters_alive": len(alive_adapters),
            "inbox_clear": queues.get("inbox_pending", 0) == 0,
        }

    return result


# ============================================================================
# REFERENCE PASSING / CLAIM CHECK (v0.8.0)
# ============================================================================
# Instead of sending full message text through the leader socket,
# the hub writes the payload to a file and sends a short reference.
# Agents read the file when they choose to.
#
# Payload dir: ~/asdaaas/payloads/
# Payload file: <msg_id>.json (same format as inbox messages)
# Reference format sent through leader:
#   [REF] ~/asdaaas/payloads/<msg_id>.json
#   [preview] first ~150 chars of message

PAYLOADS_DIR = HUB_DIR / "payloads"

PREVIEW_LENGTH = 150  # chars of message text to include in reference


def write_payload(
    msg_id: str,
    sender: str,
    to: str,
    text: str,
    adapter: str = "hub",
    meta: Optional[dict] = None,
) -> Path:
    """
    Write a message payload to disk. Returns the file path.

    The hub calls this before sending a reference through the leader.
    The payload persists on disk for agents to read when ready.
    """
    PAYLOADS_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "id": msg_id,
        "from": sender,
        "to": to,
        "text": text,
        "adapter": adapter,
        "meta": meta or {},
        "ts": time.time(),
    }

    payload_path = PAYLOADS_DIR / f"{msg_id}.json"

    # Atomic write
    fd, tmp_path = tempfile.mkstemp(dir=str(PAYLOADS_DIR), suffix=".tmp", prefix="pay_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.rename(tmp_path, str(payload_path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return payload_path


def read_payload(msg_id: str) -> Optional[dict]:
    """
    Read a payload from disk by message ID.
    Agents call this when they receive a reference and want the full content.
    """
    payload_path = PAYLOADS_DIR / f"{msg_id}.json"
    if not payload_path.exists():
        return None
    try:
        with open(payload_path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def read_payload_by_path(path: str) -> Optional[dict]:
    """
    Read a payload from disk by file path.
    Alternative when the agent has the full path from the reference notice.
    """
    expanded = os.path.expanduser(path)
    if not os.path.exists(expanded):
        return None
    try:
        with open(expanded) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def format_reference(msg_id: str, sender: str, adapter: str, text: str) -> str:
    """
    Format a short reference notice to send through the leader.

    This is what the agent sees in their context -- just the reference,
    not the full message. Keeps leader traffic and agent context small.
    """
    preview = text[:PREVIEW_LENGTH]
    if len(text) > PREVIEW_LENGTH:
        preview += "..."

    payload_path = f"~/asdaaas/payloads/{msg_id}.json"

    return preview


def cleanup_payloads(max_age_seconds: int = 3600) -> int:
    """
    Delete payload files older than max_age_seconds.
    Call periodically from the hub event loop.
    Returns number of files deleted.
    """
    if not PAYLOADS_DIR.exists():
        return 0

    now = time.time()
    deleted = 0

    for entry in PAYLOADS_DIR.iterdir():
        if not entry.name.endswith(".json"):
            continue
        try:
            age = now - entry.stat().st_mtime
            if age > max_age_seconds:
                entry.unlink()
                deleted += 1
        except OSError:
            pass

    return deleted


# ============================================================================
# AGENT UTILITIES (self-compact, status, gaze, awareness)
# ============================================================================
#
# These functions let agents manage their own infrastructure.
# An agent calls these from tool calls (bash/python).
#

SESSION_INBOX = HUB_DIR / "adapters" / "session" / "inbox"  # legacy, kept for monkeypatching


def _session_inbox(agent_name):
    """Get the session adapter inbox for an agent (agent-centric path)."""
    return AGENTS_HOME_DIR / agent_name / "asdaaas" / "adapters" / "session" / "inbox"


def request_compact(agent_name: str) -> str:
    """
    Request self-compaction. Writes a compact command to the session adapter
    inbox. The session adapter picks it up, tells asdaaas, asdaaas sends
    /compact to the agent process. Result delivered as a doorbell.

    Args:
        agent_name: The agent requesting compaction (e.g. "Trip")

    Returns:
        The request ID.
    """
    inbox = _session_inbox(agent_name)
    inbox.mkdir(parents=True, exist_ok=True)

    request_id = f"self_compact_{int(time.time())}"
    cmd = {
        "command": "compact",
        "request_id": request_id,
        "source": "self",
    }

    fd, tmp_path = tempfile.mkstemp(dir=str(inbox), suffix=".tmp", prefix="cmd_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cmd, f)
        os.rename(tmp_path, tmp_path.replace(".tmp", ".json"))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return request_id


def request_status(agent_name: str) -> str:
    """
    Request own status. Writes a status command to the session adapter inbox.
    Result delivered as a doorbell with context usage, token counts, etc.

    Args:
        agent_name: The agent requesting status (e.g. "Trip")

    Returns:
        The request ID.
    """
    inbox = _session_inbox(agent_name)
    inbox.mkdir(parents=True, exist_ok=True)

    request_id = f"self_status_{int(time.time())}"
    cmd = {
        "command": "status",
        "request_id": request_id,
        "source": "self",
    }

    fd, tmp_path = tempfile.mkstemp(dir=str(inbox), suffix=".tmp", prefix="cmd_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cmd, f)
        os.rename(tmp_path, tmp_path.replace(".tmp", ".json"))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return request_id


def set_gaze(agent_name: str, room: str, adapter: str = "irc",
             thoughts_room: str = None, thoughts_adapter: str = None):
    """
    Set the agent's gaze (where speech and thoughts go).

    Args:
        agent_name:       Agent name (e.g. "Trip")
        room:             Room for speech (e.g. "#standup", "pm:eric")
        adapter:          Adapter for speech (default "irc")
        thoughts_room:    Room for thoughts (default: #{agent}-thoughts)
        thoughts_adapter: Adapter for thoughts (default: same as speech adapter)
    """
    agent_d = AGENTS_HOME_DIR / agent_name / "asdaaas"
    agent_d.mkdir(parents=True, exist_ok=True)

    thoughts_adapter = thoughts_adapter or adapter
    if thoughts_room is None:
        thoughts_room = f"#{agent_name.lower()}-thoughts"

    gaze = {
        "speech": {"target": adapter, "params": {"room": room}},
        "thoughts": {"target": thoughts_adapter, "params": {"room": thoughts_room}},
    }

    gaze_file = agent_d / "gaze.json"
    fd, tmp_path = tempfile.mkstemp(dir=str(agent_d), suffix=".tmp", prefix="gaze_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(gaze, f)
        os.rename(tmp_path, str(gaze_file))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def set_awareness(agent_name: str, background_channels: dict = None,
                  background_default: str = "pending"):
    """
    Set the agent's awareness (what it hears in the background).

    Args:
        agent_name:          Agent name (e.g. "Trip")
        background_channels: Dict of room -> mode (e.g. {"#standup": "doorbell"})
        background_default:  Default mode for unlisted rooms ("doorbell", "pending", "drop")
    """
    agent_d = AGENTS_HOME_DIR / agent_name / "asdaaas"
    agent_d.mkdir(parents=True, exist_ok=True)

    awareness = {
        "background_channels": background_channels or {},
        "background_default": background_default,
    }

    awareness_file = agent_d / "awareness.json"
    fd, tmp_path = tempfile.mkstemp(dir=str(agent_d), suffix=".tmp", prefix="aw_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(awareness, f)
        os.rename(tmp_path, str(awareness_file))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ============================================================================
# SELF-TEST
# ============================================================================

if __name__ == "__main__":
    import sys

    print("=== Adapter API Self-Test ===")
    ensure_dirs("test_adapter")

    # Write a message to inbox
    msg_id = write_message(
        to="Sr",
        text="Hello from test adapter",
        adapter="test_adapter",
        sender="test_user",
        meta={"channel": "#test"},
    )
    print(f"1. Wrote message to inbox: {msg_id}")

    # Verify it's there
    inbox_files = list(INBOX_DIR.glob("*.json"))
    print(f"2. Inbox files: {len(inbox_files)}")

    # Read it back (simulating hub)
    for f in inbox_files:
        with open(f) as fh:
            data = json.load(fh)
            if data["id"] == msg_id:
                print(f"3. Read back: to={data['to']}, text={data['text']}")
                f.unlink()  # Clean up

    # Write a response (simulating hub)
    resp_id = write_response(
        adapter_name="test_adapter",
        request_id=msg_id,
        from_agent="Sr",
        text="Got your message!",
    )
    print(f"4. Wrote response to outbox: {resp_id}")

    # Poll responses (simulating adapter)
    responses = poll_responses("test_adapter")
    print(f"5. Polled responses: {len(responses)}")
    for r in responses:
        print(f"   from={r['from']}, text={r['text']}")

    # Cleanup
    test_outbox = OUTBOX_DIR / "test_adapter"
    if test_outbox.exists():
        import shutil
        shutil.rmtree(test_outbox)

    print("\n=== Self-test passed ===")


# ============================================================================
# PER-ADAPTER INBOX/OUTBOX (Phase 3)
# ============================================================================
# New directory structure: ~/asdaaas/adapters/<adapter>/inbox/<agent>/
#                          ~/asdaaas/adapters/<adapter>/outbox/<agent>/
#
# Adapter writes inbound messages to its inbox/<agent>/ directory.
# asdaaas reads from there, pipes to agent, writes response to outbox/<agent>/.
# Adapter reads from outbox/<agent>/ to deliver responses.

PER_ADAPTER_DIR = HUB_DIR / "adapters"


def write_to_adapter_inbox(
    adapter_name: str,
    to: str,
    text: str,
    sender: str = None,
    meta: Optional[dict] = None,
    msg_id: Optional[str] = None,
) -> str:
    """
    Write a message to an adapter's per-agent inbox.
    Called by adapters to deliver inbound messages.
    
    Path: ~/agents/<agent>/asdaaas/adapters/<adapter>/inbox/<msg_id>.json
    """
    inbox = AGENTS_HOME_DIR / to / "asdaaas" / "adapters" / adapter_name / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    
    if sender is None:
        sender = adapter_name
    msg_id = msg_id or str(uuid.uuid4())
    
    msg = {
        "id": msg_id,
        "from": sender,
        "to": to,
        "text": text,
        "adapter": adapter_name,
        "meta": meta or {},
        "ts": time.time(),
    }
    
    ts_ms = int(time.time() * 1000)
    fd, tmp_path = tempfile.mkstemp(dir=str(inbox), suffix=".tmp", prefix=f"msg_{ts_ms}_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(msg, f)
        final_path = tmp_path.replace(".tmp", ".json")
        os.rename(tmp_path, final_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    
    return msg_id


def poll_adapter_inbox(adapter_name: str, agent_name: str, delete: bool = True) -> list:
    """
    Poll an adapter's per-agent inbox for messages.
    Called by asdaaas to pick up inbound messages for an agent.
    
    Path: ~/agents/<agent>/asdaaas/adapters/<adapter>/inbox/
    """
    inbox = AGENTS_HOME_DIR / agent_name / "asdaaas" / "adapters" / adapter_name / "inbox"
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
            print(f"[adapter_api] Error reading {entry}: {e}")
    
    return messages


def write_to_adapter_outbox(
    adapter_name: str,
    agent_name: str,
    text: str,
    content_type: str = "speech",
    meta: Optional[dict] = None,
    msg_id: Optional[str] = None,
) -> str:
    """
    Write a message to an adapter's per-agent outbox.
    Called by asdaaas to deposit agent responses for adapter pickup.
    
    Path: ~/agents/<agent>/asdaaas/adapters/<adapter>/outbox/<msg_id>.json
    """
    outbox = AGENTS_HOME_DIR / agent_name / "asdaaas" / "adapters" / adapter_name / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    
    msg_id = msg_id or str(uuid.uuid4())
    
    msg = {
        "id": msg_id,
        "from": agent_name,
        "content_type": content_type,
        "text": text,
        "meta": meta or {},
        "ts": time.time(),
    }
    
    fd, tmp_path = tempfile.mkstemp(dir=str(outbox), suffix=".tmp", prefix="resp_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(msg, f)
        final_path = tmp_path.replace(".tmp", ".json")
        os.rename(tmp_path, final_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    
    return msg_id


def poll_adapter_outbox(adapter_name: str, agent_name: str, delete: bool = True) -> list:
    """
    Poll an adapter's per-agent outbox for responses.
    Called by adapters to pick up agent responses.
    
    Path: ~/agents/<agent>/asdaaas/adapters/<adapter>/outbox/
    """
    outbox = AGENTS_HOME_DIR / agent_name / "asdaaas" / "adapters" / adapter_name / "outbox"
    if not outbox.exists():
        return []
    
    responses = []
    for entry in sorted(outbox.iterdir()):
        if not entry.name.endswith(".json"):
            continue
        try:
            with open(entry, "r") as f:
                data = json.load(f)
            responses.append(data)
            if delete:
                entry.unlink()
        except json.JSONDecodeError:
            pass
        except Exception as e:
            print(f"[adapter_api] Error reading {entry}: {e}")
    
    return responses
