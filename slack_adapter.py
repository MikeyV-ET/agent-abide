#!/usr/bin/env python3
"""
MikeyV Slack Adapter -- Multi-agent Slack transport using the adapter API.
==========================================================================
One process, N Slack bot tokens -- each agent gets their own Slack presence.

Routing:
  DM messages from Eric -> parse target agent -> deliver to that agent's inbox
  Agent responses -> post from that agent's Slack bot to the DM

Architecture:
  Slack API <-- Agent Sr bot  --+
             <-- Agent Jr bot  --+
             <-- Agent Trip bot -+-- slack_adapter --> agent inboxes --> asdaaas --> agent
             <-- Agent Q bot  ---+                                        |
             <-- Agent Cinco bot-+<-- agent outboxes <--------------------+

Usage:
  python3 slack_adapter.py
  python3 slack_adapter.py --agents Sr,Trip --poll-interval 1.5
"""

import asyncio
import json
import os
import sys
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapter_api

def tprint(msg):
    """Timestamped print."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ============================================================================
# CONFIG
# ============================================================================

ADAPTER_NAME = "slack"
DEFAULT_POLL_INTERVAL = 1.0
OUTBOX_POLL_INTERVAL = 0.5
BATCH_WINDOW = 0.5
CREDS_DIR = os.path.expanduser("~/.mikeyv_creds")
try:
    from asdaaas_config import config
except ModuleNotFoundError:
    import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'core'))
    from asdaaas_config import config
DOWNLOAD_DIR = str(config.agents_home / "slack_files")
MAX_SLACK_MSG_LEN = 3900

# Per-agent Slack credentials: each agent can have their own bot token + DM channel
# Stored at ~/.mikeyv_creds/slack_<agent>_token and slack_<agent>_dm_channel
# Falls back to shared token/channel if per-agent creds don't exist.
DEFAULT_DM_CHANNEL = "D0AMVLN05PG"
DEFAULT_BOT_USER_ID = "U0ANRNVFL72"

AGENT_NAMES = ["Sr", "Jr", "Trip", "Q", "Cinco"]
AGENTS_DIR = str(config.agents_home)

AGENT_ALIASES = {
    "sr": "Sr", "senior": "Sr", "mikeyv-sr": "Sr",
    "jr": "Jr", "junior": "Jr", "mikeyv-jr": "Jr",
    "trip": "Trip", "mikeyv-trip": "Trip",
    "q": "Q", "mikeyv-q": "Q",
    "cinco": "Cinco", "mikeyv-cinco": "Cinco", "5": "Cinco",
}
BROADCAST_ALIASES = {"gang", "everyone", "all", "team", "yall"}


# ============================================================================
# AWARENESS-BASED CHANNEL WATCHING
# ============================================================================

def load_watched_channels(agent_name):
    """Read agent's awareness.json and return list of Slack DM channel IDs to watch.

    Looks for background_channels entries matching 'slack:dm:<channel_id>'.
    Returns list of channel IDs (excluding the agent's primary DM channel,
    which is already watched by default).
    """
    awareness_path = os.path.join(AGENTS_DIR, agent_name, "asdaaas", "awareness.json")
    channels = []
    try:
        if os.path.exists(awareness_path):
            with open(awareness_path) as f:
                awareness = json.load(f)
            bg = awareness.get("background_channels", {})
            for key, mode in bg.items():
                if key.startswith("slack:dm:") and mode in ("doorbell", "pending"):
                    channel_id = key.split("slack:dm:", 1)[1]
                    channels.append(channel_id)
    except (json.JSONDecodeError, IOError) as e:
        tprint(f"[slack-adapter] {agent_name}: awareness read error: {e}")
    return channels


# ============================================================================
# TARGET PARSING
# ============================================================================

def parse_target(text):
    """Parse target agent from message text. Returns agent name or 'broadcast'."""
    text_lower = text.lower().strip()
    for bcast in BROADCAST_ALIASES:
        if (text_lower.startswith(f"{bcast}:") or
            text_lower.startswith(f"{bcast},") or
            text_lower.startswith(f"{bcast} ")):
            return "broadcast"
    for alias, canonical in AGENT_ALIASES.items():
        if (text_lower.startswith(f"{alias}:") or
            text_lower.startswith(f"{alias},") or
            text_lower.startswith(f"{alias} ")):
            return canonical
    return None  # no explicit target; caller uses first agent in list


# ============================================================================
# RESPONSE CLEANING
# ============================================================================

def clean_response(text):
    """Clean agent response for Slack posting. Suppress 'noted' and strip headers."""
    if not text or not text.strip():
        return None
    cleaned = []
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("[FROM:") or s.startswith("[TO:") or s.startswith("[VIA:"):
            continue
        if s.startswith("**[FROM:") or s.startswith("**[TO:"):
            continue
        cleaned.append(line)
    result = "\n".join(cleaned).strip()
    if not result:
        return None
    alpha_only = "".join(c for c in result if c.isalpha()).lower()
    if alpha_only in ("note", "noted"):
        return None
    return result


# ============================================================================
# SLACK COMMAND PARSING
# ============================================================================

# Cache: @username -> user_id, user_id -> dm_channel_id
_user_cache = {}      # display_name/username -> user_id
_dm_cache = {}        # user_id -> dm_channel_id


def parse_slack_commands(text):
    """Parse agent output for Slack slash commands.

    Returns (commands, remaining_text) where commands is a list of
    dicts like {"type": "msg", "target": "<user>", "text": "hello"}
    and remaining_text is everything that wasn't a command.

    /msg is multiline: all lines after /msg until the next /msg or end
    of text are included in the message body.

    Supported commands:
      /msg <@user_id> <text>   - send DM to user by ID
      /msg @username <text>    - send DM to user by display name
      /msg <channel_id> <text> - send to specific channel
    """
    commands = []
    remaining = []
    current_cmd = None  # tracks active /msg command for multiline

    for line in text.split("\n"):
        stripped = line.strip()

        if stripped.startswith("/msg "):
            # Flush previous command if any
            if current_cmd is not None:
                commands.append(current_cmd)
                current_cmd = None

            parts = stripped[5:].strip().split(" ", 1)
            if len(parts) >= 1:
                target = parts[0]
                msg_text = parts[1] if len(parts) == 2 else ""
                # Strip Slack user mention formatting: <@U12345> -> U12345
                if target.startswith("<@") and target.endswith(">"):
                    target = target[2:-1]
                current_cmd = {"type": "msg", "target": target, "text": msg_text}
        elif current_cmd is not None:
            # Continuation of multiline /msg
            current_cmd["text"] += "\n" + line
        else:
            remaining.append(line)

    # Flush final command
    if current_cmd is not None:
        commands.append(current_cmd)

    remaining_text = "\n".join(remaining).strip()
    return commands, remaining_text


# ============================================================================
# SLACK CONNECTION (per-agent)
# ============================================================================

class SlackConnection:
    """Single Slack bot connection for one agent."""

    def __init__(self, agent_name, token, dm_channel, bot_user_id):
        self.agent_name = agent_name
        self.token = token
        self.dm_channel = dm_channel
        self.bot_user_id = bot_user_id
        self.last_ts = {}
        self._startup_skip = True

    async def api_call(self, method, params=None, post_data=None):
        """Make a Slack API call via curl."""
        if not self.token:
            return {"ok": False, "error": "no_token"}
        if post_data:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "-X", "POST",
                "-H", f"Authorization: Bearer {self.token}",
                "-H", "Content-Type: application/json",
                "-d", json.dumps(post_data),
                f"https://slack.com/api/{method}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            query = "&".join(f"{k}={v}" for k, v in (params or {}).items())
            url = f"https://slack.com/api/{method}?{query}" if query else f"https://slack.com/api/{method}"
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s",
                "-H", f"Authorization: Bearer {self.token}",
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        stdout, _ = await proc.communicate()
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": "json_decode_error", "raw": stdout.decode()[:200]}

    async def download_file(self, file_info):
        """Download a Slack file to local disk. Returns local path or None."""
        url = file_info.get("url_private_download") or file_info.get("url_private")
        if not url:
            return None
        fname = file_info.get("name", f"file_{file_info.get('id', 'unknown')}")
        # Sanitize filename
        fname = fname.replace("/", "_").replace("..", "_")
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        # Add timestamp prefix to avoid collisions
        local_path = os.path.join(DOWNLOAD_DIR, f"{int(time.time())}_{fname}")
        try:
            proc = await asyncio.create_subprocess_exec(
                "curl", "-s", "-L",
                "-H", f"Authorization: Bearer {self.token}",
                "-o", local_path,
                url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
                tprint(f"[slack-adapter] Downloaded: {fname} -> {local_path} ({os.path.getsize(local_path)} bytes)")
                return local_path
            else:
                tprint(f"[slack-adapter] Download failed (empty): {fname}")
                return None
        except Exception as e:
            tprint(f"[slack-adapter] Download error for {fname}: {e}")
            return None

    async def poll_dm(self, channel_id=None):
        """Poll a DM channel for new messages. Returns list oldest first."""
        channel_id = channel_id or self.dm_channel
        oldest = self.last_ts.get(channel_id)
        params = {"channel": channel_id, "limit": "5"}
        if oldest is not None:
            params["oldest"] = oldest
        data = await self.api_call("conversations.history", params=params)
        if not data.get("ok"):
            if data.get("error") != "no_token":
                tprint(f"[slack-adapter] {self.agent_name} poll error: {data.get('error')}")
            return []
        messages = data.get("messages", [])
        new_messages = []
        for msg in messages:
            if msg.get("user") == self.bot_user_id:
                continue
            if msg.get("bot_id"):
                continue
            if msg.get("subtype"):
                continue

            # Build message text with file/attachment info
            text = msg.get("text", "")
            files = msg.get("files", [])
            downloaded_files = []

            if files:
                for f in files:
                    local_path = await self.download_file(f)
                    if local_path:
                        downloaded_files.append({
                            "name": f.get("name", "unknown"),
                            "mimetype": f.get("mimetype", "unknown"),
                            "size": f.get("size", 0),
                            "local_path": local_path,
                        })
                        text += f"\n[Attached file: {f.get('name', 'unknown')} ({f.get('mimetype', '?')}, {f.get('size', 0)} bytes) -> {local_path}]"
                    else:
                        text += f"\n[Attached file: {f.get('name', 'unknown')} ({f.get('mimetype', '?')}) — download failed]"

            # Include attachment previews (link unfurls etc.)
            attachments = msg.get("attachments", [])
            if attachments:
                for att in attachments:
                    att_text = att.get("text") or att.get("fallback") or att.get("title", "")
                    if att_text:
                        text += f"\n[Link preview: {att_text[:200]}]"

            new_messages.append({
                "user": msg.get("user", "unknown"),
                "text": text,
                "ts": msg.get("ts", ""),
                "channel_id": channel_id,
                "files": downloaded_files,
            })
        if messages:
            latest_ts = max(msg.get("ts", "0") for msg in messages)
            self.last_ts[channel_id] = latest_ts
        if self._startup_skip:
            self._startup_skip = False
            if new_messages:
                tprint(f"[slack-adapter] {self.agent_name} startup: skipping {len(new_messages)} old message(s)")
            return []
        new_messages.reverse()
        return new_messages

    async def add_reaction(self, channel_id, timestamp, emoji):
        """Add an emoji reaction to a message."""
        data = await self.api_call("reactions.add", post_data={
            "channel": channel_id,
            "timestamp": timestamp,
            "name": emoji,
        })
        if not data.get("ok") and data.get("error") != "already_reacted":
            tprint(f"[slack-adapter] {self.agent_name} reaction add failed: {data.get('error')}")

    async def remove_reaction(self, channel_id, timestamp, emoji):
        """Remove an emoji reaction from a message."""
        data = await self.api_call("reactions.remove", post_data={
            "channel": channel_id,
            "timestamp": timestamp,
            "name": emoji,
        })
        if not data.get("ok") and data.get("error") != "no_reaction":
            tprint(f"[slack-adapter] {self.agent_name} reaction remove failed: {data.get('error')}")

    async def post_message(self, text, channel_id=None):
        """Post a message to a Slack channel/DM."""
        channel_id = channel_id or self.dm_channel
        if len(text) > MAX_SLACK_MSG_LEN:
            text = text[:MAX_SLACK_MSG_LEN] + "\n... (truncated)"
        data = await self.api_call("chat.postMessage", post_data={
            "channel": channel_id,
            "text": text,
        })
        if not data.get("ok"):
            tprint(f"[slack-adapter] {self.agent_name} post failed: {data.get('error')}")
            return False
        return True

    async def resolve_user(self, target):
        """Resolve a target to a user ID.

        Target can be:
          - A user ID (U...) -> returned as-is
          - @username or @display_name -> looked up via users.list
        """
        global _user_cache
        # Already a user ID
        if target.startswith("U") and len(target) > 5:
            return target
        # Strip leading @
        name = target.lstrip("@").lower()
        # Check cache
        if name in _user_cache:
            return _user_cache[name]
        # Fetch user list and populate cache
        data = await self.api_call("users.list")
        if data.get("ok"):
            for member in data.get("members", []):
                uid = member["id"]
                profile = member.get("profile", {})
                for field in [member.get("name", ""), profile.get("display_name", ""),
                              profile.get("real_name", "")]:
                    if field:
                        _user_cache[field.lower()] = uid
        return _user_cache.get(name)

    async def open_dm(self, user_id):
        """Open (or get existing) DM channel with a user. Returns channel ID."""
        global _dm_cache
        if user_id in _dm_cache:
            return _dm_cache[user_id]
        data = await self.api_call("conversations.open", post_data={
            "users": user_id,
        })
        if data.get("ok"):
            channel_id = data["channel"]["id"]
            _dm_cache[user_id] = channel_id
            tprint(f"[slack-adapter] Opened DM with {user_id}: {channel_id}")
            return channel_id
        else:
            tprint(f"[slack-adapter] Failed to open DM with {user_id}: {data.get('error')}")
            return None

    async def send_dm(self, target, text):
        """Send a DM to a user by ID, @username, or channel ID.

        Resolves the target, opens the DM channel if needed, and posts.
        Returns True on success.
        """
        # If target looks like a channel ID (starts with D or C), post directly
        if target.startswith("D") or target.startswith("C"):
            return await self.post_message(text, channel_id=target)
        # Otherwise resolve to user ID then open DM
        user_id = await self.resolve_user(target)
        if not user_id:
            tprint(f"[slack-adapter] Could not resolve user: {target}")
            return False
        channel_id = await self.open_dm(user_id)
        if not channel_id:
            return False
        return await self.post_message(text, channel_id=channel_id)


# ============================================================================
# CREDENTIAL LOADING
# ============================================================================

def load_agent_creds(agent_name):
    """Load per-agent Slack credentials, falling back to shared creds."""
    # Try per-agent token
    agent_token_path = os.path.join(CREDS_DIR, f"slack_{agent_name.lower()}_token")
    shared_token_path = os.path.join(CREDS_DIR, "slack_bot_token")

    token = None
    if os.path.exists(agent_token_path):
        with open(agent_token_path) as f:
            token = f.read().strip()
        tprint(f"[slack-adapter] {agent_name}: per-agent token from {agent_token_path}")
    elif os.path.exists(shared_token_path):
        with open(shared_token_path) as f:
            token = f.read().strip()
        tprint(f"[slack-adapter] {agent_name}: shared token from {shared_token_path}")

    # Try per-agent DM channel
    agent_dm_path = os.path.join(CREDS_DIR, f"slack_{agent_name.lower()}_dm_channel")
    dm_channel = DEFAULT_DM_CHANNEL
    if os.path.exists(agent_dm_path):
        with open(agent_dm_path) as f:
            dm_channel = f.read().strip()
        tprint(f"[slack-adapter] {agent_name}: per-agent DM channel {dm_channel}")

    # Try per-agent bot user ID
    agent_uid_path = os.path.join(CREDS_DIR, f"slack_{agent_name.lower()}_bot_user_id")
    bot_user_id = DEFAULT_BOT_USER_ID
    if os.path.exists(agent_uid_path):
        with open(agent_uid_path) as f:
            bot_user_id = f.read().strip()

    return token, dm_channel, bot_user_id


# ============================================================================
# MAIN LOOP
# ============================================================================

async def run_adapter(agents, poll_interval, startup_skip=True):
    """Main adapter loop: poll Slack DMs + poll agent outboxes."""
    adapter_api.ensure_dirs(ADAPTER_NAME)

    connections = {}
    agent_list = list(agents)

    tprint(f"[slack-adapter] Starting multi-agent Slack adapter")
    tprint(f"[slack-adapter]   Agents: {', '.join(agent_list)}")
    tprint(f"[slack-adapter]   Poll interval: {poll_interval}s")

    # Create per-agent connections
    for agent_name in agent_list:
        token, dm_channel, bot_user_id = load_agent_creds(agent_name)
        if not token:
            tprint(f"[slack-adapter] {agent_name}: NO TOKEN - skipping")
            continue
        conn = SlackConnection(agent_name, token, dm_channel, bot_user_id)
        conn._startup_skip = startup_skip
        connections[agent_name] = conn
        tprint(f"[slack-adapter] {agent_name}: connected (DM: {dm_channel})")

    if not connections:
        tprint("[slack-adapter] FATAL: No agents with valid tokens. Exiting.")
        sys.exit(1)

    tprint(f"[slack-adapter] {len(connections)} agent(s) online. Running.")

    _last_heartbeat = time.time()

    # Register adapter
    adapter_api.register_adapter(
        name=ADAPTER_NAME,
        capabilities=["send", "receive", "broadcast"],
        config={
            "type": "direct",
            "agents": agent_list,
            "poll_interval": poll_interval,
        },
    )

    # Use first connected agent as the DM listener (for shared-token mode)
    # In per-agent mode, each agent polls their own DM
    listener_agent = agent_list[0]
    seen_dm_channels = set()

    # Awareness-based channel watching: reload every N cycles
    _awareness_reload_interval = 30  # reload awareness every 30 poll cycles (~15s)
    _awareness_cycle = 0
    _extra_channels = {}  # agent_name -> [channel_ids from awareness.json]

    # Pending reactions: track messages we've reacted 👀 to, so we can ✅ on response
    _pending_reactions = {}  # agent_name -> [(channel_id, ts), ...]

    while True:
        # ---- 0. Periodically reload awareness-based watched channels ----
        if _awareness_cycle % _awareness_reload_interval == 0:
            for agent_name in connections:
                watched = load_watched_channels(agent_name)
                # Filter out the agent's primary DM (already polled)
                primary = connections[agent_name].dm_channel
                extra = [ch for ch in watched if ch != primary]
                if extra != _extra_channels.get(agent_name, []):
                    _extra_channels[agent_name] = extra
                    if extra:
                        tprint(f"[slack-adapter] {agent_name}: watching {len(extra)} extra channel(s) from awareness")
        _awareness_cycle += 1

        # ---- 1. Poll Slack DMs for inbound messages ----
        for agent_name, conn in connections.items():
            # In shared-token mode, only poll once per unique DM channel
            if conn.dm_channel in seen_dm_channels and conn.token == connections[listener_agent].token:
                continue
            seen_dm_channels.add(conn.dm_channel)

            # Build list of channels to poll: primary DM + awareness extras
            channels_to_poll = [conn.dm_channel] + _extra_channels.get(agent_name, [])

            for poll_channel in channels_to_poll:
                if poll_channel != conn.dm_channel and poll_channel in seen_dm_channels:
                    continue
                seen_dm_channels.add(poll_channel)

                try:
                    messages = await conn.poll_dm(channel_id=poll_channel)
                    for msg in messages:
                        target = parse_target(msg["text"])
                        tprint(f"[slack-adapter] Inbound via {agent_name} (ch:{poll_channel[:8]}): target={target} text={msg['text'][:80]}")

                        # Add 👀 reaction to acknowledge receipt
                        if msg.get("ts"):
                            await conn.add_reaction(poll_channel, msg["ts"], "eyes")

                        if target == "broadcast":
                            targets = list(connections.keys())
                        elif target and target in connections:
                            targets = [target]
                        elif poll_channel != conn.dm_channel:
                            # Messages from awareness channels go to the watching agent
                            targets = [agent_name]
                        else:
                            targets = [listener_agent]

                        for tgt in targets:
                            try:
                                msg_id = adapter_api.write_to_adapter_inbox(
                                    adapter_name=ADAPTER_NAME,
                                    to=tgt,
                                    text=f"[SLACK DM from {msg['user']}]\n{msg['text']}",
                                    sender=msg["user"],
                                    meta={
                                        "room": poll_channel,
                                        "channel_id": poll_channel,
                                        "slack_ts": msg.get("ts", ""),
                                        "slack_user": msg.get("user", ""),
                                    },
                                )
                                tprint(f"[slack-adapter] {msg['user']} -> {tgt} (id: {msg_id[:8]})")
                                # Track for ✅ reaction on response
                                if msg.get("ts"):
                                    _pending_reactions.setdefault(tgt, []).append(
                                        (poll_channel, msg["ts"])
                                    )
                            except Exception as e:
                                tprint(f"[slack-adapter] Inbox write error for {tgt}: {e}")
                except Exception as e:
                    tprint(f"[slack-adapter] {agent_name} poll error (ch:{poll_channel[:8]}): {e}")

        seen_dm_channels.clear()

        # ---- 2. Poll agent outboxes for responses -> Slack ----
        try:
            for agent_name, conn in connections.items():
                responses = adapter_api.poll_adapter_outbox(ADAPTER_NAME, agent_name)
                for resp in responses:
                    content_type = resp.get("content_type", "speech")
                    raw_text = resp.get("text", "")
                    text = clean_response(raw_text)

                    if text is None:
                        raw_alpha = "".join(c for c in raw_text if c.isalpha()).lower()
                        if raw_alpha in ("note", "noted"):
                            tprint(f"[slack-adapter] {agent_name} suppressed (noted)")
                        continue

                    if content_type == "thoughts":
                        # Skip thoughts for Slack (or could go to a separate channel)
                        tprint(f"[slack-adapter] {agent_name} thoughts skipped ({len(text)} chars)")
                        continue

                    # Parse slash commands (e.g., /msg @user hello)
                    commands, remaining = parse_slack_commands(text)

                    # Execute /msg commands
                    for cmd in commands:
                        if cmd["type"] == "msg":
                            success = await conn.send_dm(cmd["target"], cmd["text"])
                            tprint(f"[slack-adapter] {agent_name} /msg {cmd['target']} ({len(cmd['text'])} chars) ok={success}")

                    # Post remaining text to default target
                    if remaining:
                        target_channel = resp.get("channel_id") or resp.get("meta", {}).get("channel_id") or conn.dm_channel
                        await conn.post_message(remaining, target_channel)
                        tprint(f"[slack-adapter] {agent_name} -> Slack ({len(remaining)} chars)")

                    # Mark pending messages as handled: 👀 -> ✅
                    pending = _pending_reactions.pop(agent_name, [])
                    for ch_id, msg_ts in pending:
                        await conn.remove_reaction(ch_id, msg_ts, "eyes")
                        await conn.add_reaction(ch_id, msg_ts, "white_check_mark")

        except Exception as e:
            tprint(f"[slack-adapter] Outbox error: {e}")
            import traceback
            traceback.print_exc()

        # ---- 3. Periodic heartbeat ----
        _now = time.time()
        if _now - _last_heartbeat >= 30:
            adapter_api.update_heartbeat(ADAPTER_NAME)
            _last_heartbeat = _now

        await asyncio.sleep(OUTBOX_POLL_INTERVAL)


def main():
    parser = argparse.ArgumentParser(description="MikeyV Slack Adapter")
    parser.add_argument("--agents", default=None,
                        help="Comma-separated agent subset (default: all)")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL,
                        help=f"Slack poll interval in seconds (default: {DEFAULT_POLL_INTERVAL})")
    parser.add_argument("--no-startup-skip", action="store_true",
                        help="Process old messages on startup (default: skip)")
    args = parser.parse_args()

    if args.agents:
        agent_list = [a.strip() for a in args.agents.split(",")]
    else:
        agent_list = list(AGENT_NAMES)

    try:
        asyncio.run(run_adapter(
            agents=agent_list,
            poll_interval=args.poll_interval,
            startup_skip=not args.no_startup_skip,
        ))
    except KeyboardInterrupt:
        print("\n[slack-adapter] Shutting down.")


if __name__ == "__main__":
    main()
