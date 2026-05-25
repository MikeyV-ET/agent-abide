#!/usr/bin/env python3
"""
Slack Research Adapter -- On-demand Slack exploration for agents.
=================================================================
Control adapter that lets any agent search and read Slack channels
using Eric's user token (xoxp-). Agents write command JSON to their
outbox, adapter executes against Slack API, writes results to inbox.

Commands:
  {"command": "search", "query": "hackathon", "count": 20}
  {"command": "history", "channel": "#general", "limit": 50, "topic": "hackathon"}
  {"command": "thread", "channel": "#general", "ts": "1774388146.705699"}
  {"command": "channels"}
  {"command": "status"}

Architecture (asdaaas-native):
  Agent writes:  ~/agents/<agent>/asdaaas/adapters/slack_research/outbox/<cmd>.json
  Adapter reads outbox, executes, writes result to:
                 ~/agents/<agent>/asdaaas/adapters/slack_research/inbox/<result>.json
  asdaaas delivers result as doorbell to agent.

Usage:
  python3 slack_research_adapter.py
  python3 slack_research_adapter.py --agents Cinco,Sr --poll-interval 1.0
"""

import json
import os
import sys
import time
import signal
import argparse
import subprocess
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_api

# ============================================================================
# CONFIG
# ============================================================================

ADAPTER_NAME = "slack_research"
CREDS_DIR = os.path.expanduser("~/.mikeyv_creds")
AGENTS_HOME = Path(os.path.expanduser("~/agents"))
DEFAULT_AGENTS = ["Sr", "Jr", "Trip", "Q", "Cinco"]

_start_time = time.time()
_command_count = 0
_running = True


def tprint(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_user_token():
    """Load Slack user token (xoxp-) for channel access."""
    path = os.path.join(CREDS_DIR, "slack_token")
    try:
        with open(path) as f:
            token = f.read().strip()
        if token.startswith("xoxp-"):
            return token
        tprint(f"WARNING: {path} doesn't look like a user token (expected xoxp-)")
        return token
    except FileNotFoundError:
        tprint(f"FATAL: No user token at {path}")
        return None


def slack_api(token, method, params=None):
    """Synchronous Slack API call via curl."""
    if params:
        from urllib.parse import quote
        query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        url = f"https://slack.com/api/{method}?{query}"
    else:
        url = f"https://slack.com/api/{method}"
    try:
        r = subprocess.run(
            ["curl", "-s", "-H", f"Authorization: Bearer {token}", url],
            capture_output=True, timeout=15, text=True,
        )
        return json.loads(r.stdout)
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ============================================================================
# CHANNEL NAME RESOLUTION
# ============================================================================

_channel_cache = {}  # name -> id
_cache_time = 0


def resolve_channel(token, channel_ref):
    """Resolve a channel name (e.g. '#general') to a channel ID."""
    global _channel_cache, _cache_time
    # Already an ID
    if channel_ref.startswith("C") and len(channel_ref) > 8:
        return channel_ref
    # Strip # prefix
    name = channel_ref.lstrip("#").lower()
    # Refresh cache every 5 minutes
    if time.time() - _cache_time > 300 or not _channel_cache:
        data = slack_api(token, "conversations.list",
                         {"types": "public_channel,private_channel", "limit": "200"})
        if data.get("ok"):
            _channel_cache = {ch["name"].lower(): ch["id"] for ch in data.get("channels", [])}
            _cache_time = time.time()
    return _channel_cache.get(name)


# ============================================================================
# COMMAND HANDLERS
# ============================================================================

def cmd_search(token, params):
    """Search messages across all channels."""
    query = params.get("query", "")
    if not query:
        return {"status": "error", "error": "Missing 'query' parameter"}
    count = min(int(params.get("count", 20)), 100)
    sort = params.get("sort", "timestamp")
    sort_dir = params.get("sort_dir", "desc")

    data = slack_api(token, "search.messages", {
        "query": query, "count": str(count),
        "sort": sort, "sort_dir": sort_dir,
    })
    if not data.get("ok"):
        return {"status": "error", "error": data.get("error", "unknown")}

    matches = data.get("messages", {}).get("matches", [])
    total = data.get("messages", {}).get("total", 0)
    results = []
    for m in matches:
        results.append({
            "channel": m.get("channel", {}).get("name", "?"),
            "channel_id": m.get("channel", {}).get("id", "?"),
            "user": m.get("username", "?"),
            "text": m.get("text", ""),
            "ts": m.get("ts", ""),
            "permalink": m.get("permalink", ""),
        })
    return {"status": "ok", "total": total, "returned": len(results), "matches": results}


def cmd_history(token, params):
    """Read channel history, optionally filtered by topic keyword."""
    channel_ref = params.get("channel", "")
    if not channel_ref:
        return {"status": "error", "error": "Missing 'channel' parameter"}
    channel_id = resolve_channel(token, channel_ref)
    if not channel_id:
        return {"status": "error", "error": f"Cannot resolve channel: {channel_ref}"}

    limit = min(int(params.get("limit", 50)), 200)
    topic = params.get("topic", "").lower()
    oldest = params.get("oldest", "")

    api_params = {"channel": channel_id, "limit": str(limit)}
    if oldest:
        api_params["oldest"] = oldest

    data = slack_api(token, "conversations.history", api_params)
    if not data.get("ok"):
        return {"status": "error", "error": data.get("error", "unknown")}

    messages = data.get("messages", [])
    messages.reverse()  # chronological order

    if topic:
        messages = [m for m in messages if topic in m.get("text", "").lower()]

    results = []
    for m in messages:
        results.append({
            "user": m.get("user", "?"),
            "text": m.get("text", ""),
            "ts": m.get("ts", ""),
            "thread_ts": m.get("thread_ts", ""),
            "reply_count": m.get("reply_count", 0),
        })
    return {
        "status": "ok",
        "channel": channel_ref,
        "channel_id": channel_id,
        "total": len(results),
        "has_more": data.get("has_more", False),
        "messages": results,
    }


def cmd_thread(token, params):
    """Read a thread's replies."""
    channel_ref = params.get("channel", "")
    ts = params.get("ts", "")
    if not channel_ref or not ts:
        return {"status": "error", "error": "Missing 'channel' and/or 'ts' parameter"}
    channel_id = resolve_channel(token, channel_ref)
    if not channel_id:
        return {"status": "error", "error": f"Cannot resolve channel: {channel_ref}"}

    data = slack_api(token, "conversations.replies", {
        "channel": channel_id, "ts": ts, "limit": "200",
    })
    if not data.get("ok"):
        return {"status": "error", "error": data.get("error", "unknown")}

    messages = data.get("messages", [])
    results = []
    for m in messages:
        results.append({
            "user": m.get("user", "?"),
            "text": m.get("text", ""),
            "ts": m.get("ts", ""),
        })
    return {"status": "ok", "channel": channel_ref, "thread_ts": ts, "replies": results}


def cmd_channels(token, params):
    """List channels the user has access to."""
    data = slack_api(token, "conversations.list", {
        "types": "public_channel,private_channel",
        "limit": "200",
    })
    if not data.get("ok"):
        return {"status": "error", "error": data.get("error", "unknown")}

    channels = []
    for ch in data.get("channels", []):
        channels.append({
            "id": ch.get("id"),
            "name": ch.get("name"),
            "is_member": ch.get("is_member", False),
            "num_members": ch.get("num_members", 0),
            "topic": ch.get("topic", {}).get("value", ""),
            "purpose": ch.get("purpose", {}).get("value", ""),
        })
    return {"status": "ok", "channels": channels}


def cmd_status(token, params):
    """Adapter status."""
    return {
        "status": "ok",
        "adapter": ADAPTER_NAME,
        "uptime_s": int(time.time() - _start_time),
        "commands_handled": _command_count,
        "has_token": token is not None,
    }


COMMANDS = {
    "search": cmd_search,
    "history": cmd_history,
    "thread": cmd_thread,
    "channels": cmd_channels,
    "status": cmd_status,
}


# ============================================================================
# MAIN LOOP
# ============================================================================

def handle_command(token, agent_name, msg):
    """Parse and execute a command, write result to agent's inbox."""
    global _command_count
    _command_count += 1

    text = msg.get("text", "")
    msg_id = msg.get("id", "unknown")

    # Parse command JSON
    try:
        cmd = json.loads(text) if isinstance(text, str) else text
    except json.JSONDecodeError:
        cmd = {"command": text.strip()}

    command = cmd.get("command", cmd.get("action", ""))
    params = {k: v for k, v in cmd.items() if k not in ("command", "action")}

    tprint(f"CMD from {agent_name}: {command} {params}")

    handler = COMMANDS.get(command)
    if not handler:
        result = {"status": "error", "error": f"Unknown command: {command}",
                  "available": list(COMMANDS.keys())}
    else:
        try:
            result = handler(token, params)
        except Exception as e:
            result = {"status": "error", "error": str(e)}
            tprint(f"ERROR: {e}")

    result["command"] = command
    result["adapter"] = ADAPTER_NAME
    result["request_id"] = msg_id
    result["ts"] = time.time()

    # Write result to agent's adapter inbox (asdaaas delivers as doorbell)
    try:
        adapter_api.write_to_adapter_inbox(
            adapter_name=ADAPTER_NAME,
            to=agent_name,
            text=json.dumps(result),
            sender=ADAPTER_NAME,
            meta={"type": "response", "request_id": msg_id},
        )
        tprint(f"RESULT -> {agent_name}: {result.get('status')} ({command})")
    except Exception as e:
        tprint(f"ERROR writing result to {agent_name}: {e}")


def run_adapter(agents, poll_interval):
    """Main loop: poll agent outboxes for commands."""
    global _running

    token = load_user_token()
    if not token:
        tprint("FATAL: No Slack user token. Exiting.")
        sys.exit(1)

    adapter_api.register_adapter(
        name=ADAPTER_NAME,
        capabilities=["search", "history", "thread", "channels"],
        config={"type": "control", "agents": agents},
    )

    tprint(f"Registered. Polling {len(agents)} agent(s): {', '.join(agents)}")
    tprint(f"Poll interval: {poll_interval}s")

    last_heartbeat = time.time()

    while _running:
        for agent_name in agents:
            try:
                commands = adapter_api.poll_adapter_outbox(ADAPTER_NAME, agent_name)
                for cmd in commands:
                    handle_command(token, agent_name, cmd)
            except Exception as e:
                tprint(f"Error polling {agent_name}: {e}")

        now = time.time()
        if now - last_heartbeat >= 60:
            adapter_api.update_heartbeat(ADAPTER_NAME)
            last_heartbeat = now

        time.sleep(poll_interval)

    adapter_api.deregister_adapter(ADAPTER_NAME)
    tprint("Deregistered. Goodbye.")


def signal_handler(sig, frame):
    global _running
    tprint("Shutting down...")
    _running = False


def main():
    parser = argparse.ArgumentParser(description="MikeyV Slack Research Adapter")
    parser.add_argument("--agents", default=None,
                        help="Comma-separated agent list (default: all)")
    parser.add_argument("--poll-interval", type=float, default=1.0,
                        help="Outbox poll interval in seconds (default: 1.0)")
    args = parser.parse_args()

    agents = [a.strip() for a in args.agents.split(",")] if args.agents else DEFAULT_AGENTS

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    tprint("=" * 50)
    tprint(f"MikeyV Slack Research Adapter")
    tprint("=" * 50)

    run_adapter(agents, args.poll_interval)


if __name__ == "__main__":
    main()
