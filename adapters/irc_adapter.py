#!/usr/bin/env python3
"""
MikeyV IRC Adapter — Multi-nick IRC bridge using the adapter API.
=================================================================
One process, N IRC connections — each agent gets their own nick.

Routing:
  Channel messages (#standup) → broadcast to ALL agents
  Private messages (/msg MikeyV-Sr) → that agent only

That's it. IRC handles targeting. No text parsing.

Architecture:
  miniircd <── MikeyV-Sr ──┐
           <── MikeyV-Jr ──┤
           <── MikeyV-Trip ┼── irc_adapter ──> hub inbox ──> hub ──> leader
           <── MikeyV-Q ───┤                                  │
           <── MikeyV-Cinco┘◄── hub outbox/irc/ ◄─────────────┘

Usage:
  python3 irc_adapter.py
  python3 irc_adapter.py --agents Sr,Jr,Trip
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
    import time
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ============================================================================
# CONFIG
# ============================================================================

DEFAULT_CHANNEL = "#standup"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6667
ADAPTER_NAME = "irc"
OUTBOX_POLL_INTERVAL = 0.5
BATCH_WINDOW = 0.3  # quiet window: flush after 0.3s of no new messages

AGENT_NICKS = {
    "Sr": "Sr",
    "Jr": "Jr",
    "Trip": "trip",
    "Q": "Q",
    "Cinco": "Cinco",
}

# All MikeyV nicks (for loop suppression)
MIKEYV_NICKS = {v.lower() for v in AGENT_NICKS.values()}

# Per-agent thought channels
THOUGHT_CHANNELS = {
    "Sr": "#sr-thoughts",
    "Jr": "#jr-thoughts",
    "Trip": "#trip-thoughts",
    "Q": "#q-thoughts",
    "Cinco": "#cinco-thoughts",
}

# Track which channels we've joined (to avoid re-joining)
_joined_channels = set()

# Agents dir (for reading awareness files)
AGENTS_DIR = os.environ.get("MIKEYV_AGENTS_DIR", os.path.expanduser("~/agents"))


_awareness_cache = {}  # agent_name -> list of channels
_awareness_cache_ts = 0


def refresh_awareness_cache(agents):
    """Re-read all agents' awareness.json files. Called every 30s."""
    global _awareness_cache, _awareness_cache_ts
    for agent_name in agents:
        path = os.path.join(AGENTS_DIR, agent_name, "asdaaas", "awareness.json")
        try:
            with open(path) as f:
                data = json.load(f)
            _awareness_cache[agent_name] = [ch for ch in data.get("background_channels", {}) if ch.startswith("#")]
        except (FileNotFoundError, json.JSONDecodeError):
            _awareness_cache[agent_name] = []
    _awareness_cache_ts = time.time()


def get_awareness_channels(agent_name):
    """Return cached IRC channels from agent's awareness config."""
    return _awareness_cache.get(agent_name, [])


# ============================================================================
# IRC CONNECTION
# ============================================================================

class IRCConnection:
    """Single IRC connection for one agent nick."""

    def __init__(self, nick, channel, host, port, agent_name):
        self.nick = nick
        self.channel = channel
        self.host = host
        self.port = port
        self.agent_name = agent_name
        self.reader = None
        self.writer = None
        self.connected = False
        self._buffer = ""

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        self.writer.write(f"NICK {self.nick}\r\n".encode())
        self.writer.write(f"USER {self.nick.lower().replace('-','')} 0 * :{self.nick}\r\n".encode())
        await self.writer.drain()
        await asyncio.sleep(0.5)
        try:
            await asyncio.wait_for(self.reader.read(4096), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        self.connected = True

    async def join(self, channel=None):
        channel = channel or self.channel
        self.writer.write(f"JOIN {channel}\r\n".encode())
        await self.writer.drain()
        await asyncio.sleep(0.3)
        try:
            await asyncio.wait_for(self.reader.read(4096), timeout=1.0)
        except asyncio.TimeoutError:
            pass

    async def send(self, message, target=None):
        target = target or self.channel
        for line in message.split("\n"):
            line = line.strip()
            if not line:
                continue
            max_len = 400
            while len(line) > max_len:
                self.writer.write(f"PRIVMSG {target} :{line[:max_len]}\r\n".encode())
                await self.writer.drain()
                line = line[max_len:]
                await asyncio.sleep(0.1)
            self.writer.write(f"PRIVMSG {target} :{line}\r\n".encode())
            await self.writer.drain()
            await asyncio.sleep(0.1)

    async def change_nick(self, new_nick):
        self.writer.write(f"NICK {new_nick}\r\n".encode())
        await self.writer.drain()
        old = self.nick
        self.nick = new_nick
        tprint(f"[irc-adapter] {self.agent_name} nick: {old} -> {new_nick}")

    async def poll(self):
        """Returns list of {sender, target, text, is_pm}."""
        messages = []
        if not self.connected:
            return messages
        try:
            data = await asyncio.wait_for(self.reader.read(4096), timeout=0.1)
            if not data:
                self.connected = False
                return messages
            self._buffer += data.decode("utf-8", errors="replace")
            while "\r\n" in self._buffer:
                line, self._buffer = self._buffer.split("\r\n", 1)
                line = line.strip()
                if not line:
                    continue
                if line.startswith("PING"):
                    self.writer.write(f"{line.replace('PING','PONG',1)}\r\n".encode())
                    await self.writer.drain()
                    continue
                if "PRIVMSG" in line:
                    try:
                        prefix = line[1:line.index(" ")]
                        sender = prefix.split("!")[0]
                        parts = line.split(" ", 3)
                        target = parts[2]
                        msg_text = parts[3][1:]
                        if sender.lower() == self.nick.lower():
                            continue  # suppress self-echo only
                        messages.append({
                            "sender": sender,
                            "target": target,
                            "text": msg_text,
                            "is_pm": not target.startswith("#"),
                        })
                    except (ValueError, IndexError):
                        pass
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            tprint(f"[irc-adapter] {self.agent_name} poll error: {e}")
        return messages

    async def join_if_needed(self, channel):
        """Join a channel if not already joined."""
        if channel in _joined_channels:
            return
        await self.join(channel)
        _joined_channels.add(channel)
        tprint(f"[irc-adapter] {self.agent_name} joined {channel}")

    async def close(self):
        if self.writer:
            try:
                self.writer.write(f"QUIT :Adapter shutting down\r\n".encode())
                await self.writer.drain()
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        self.connected = False

    async def reconnect(self):
        await self.close()
        await asyncio.sleep(2)
        try:
            await self.connect()
            await self.join()
            tprint(f"[irc-adapter] {self.agent_name} reconnected as {self.nick}")
        except Exception as e:
            tprint(f"[irc-adapter] {self.agent_name} reconnect failed: {e}")


# ============================================================================
# BATCHER (per-agent)
# ============================================================================

class MessageBatcher:
    """Batch IRC messages per agent, flushing after a quiet period.
    
    The window is a QUIET window, not a fixed window. Every new message
    resets the timer. The batch flushes only after no new messages have
    arrived for `window` seconds. This ensures multi-line messages
    (split by IRC's 400-char limit) are delivered as one batch even if
    the lines arrive with small gaps between them.
    """
    def __init__(self, window=BATCH_WINDOW):
        self.window = window
        self.buckets = {}
        self.last_activity = {}

    def add(self, agent_name, msg):
        if agent_name not in self.buckets:
            self.buckets[agent_name] = []
        self.buckets[agent_name].append(msg)
        self.last_activity[agent_name] = time.time()

    def ready_agents(self):
        now = time.time()
        return [a for a, t in self.last_activity.items() if now - t >= self.window]

    def flush(self, agent_name):
        msgs = self.buckets.pop(agent_name, [])
        self.last_activity.pop(agent_name, None)
        return msgs


# ============================================================================
# RESPONSE CLEANING
# ============================================================================

def clean_response(text):
    if not text or not text.strip():
        return None
    # Strip headers FIRST
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
    # THEN check for suppression tokens (case-insensitive, ignoring punctuation/whitespace)
    # Agents respond "noted" (or "Noted.", "noted!", etc.) to ack messages not for them.
    # This must be suppressed before IRC to prevent noted loops.
    token = result.strip()
    # Strip all non-alpha characters to catch "Noted.", "noted!", "Noted...", etc.
    alpha_only = "".join(c for c in token if c.isalpha()).lower()
    if alpha_only in ("note", "noted"):
        return None
    # No hard truncation — the IRC send() method handles chunking at 400 chars/line.
    # Thought traces can be 10K+. Let them through.
    return result



# ============================================================================
# IRC COMMAND PARSING
# ============================================================================

def parse_irc_commands(text):
    """Parse agent output for IRC slash commands.

    Returns (commands, remaining_text) where commands is a list of
    dicts like {"type": "nick", "args": "NewName"} and remaining_text
    is everything that wasn't a command (to send as regular message).

    Supported commands:
      /nick <name>         - change nick
      /msg <target> <text> - send private message
      /join <channel>      - join channel
      /part <channel>      - leave channel
      /me <action>         - action message (CTCP ACTION)
    """
    commands = []
    remaining = []

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            remaining.append(line)
            continue

        if stripped.startswith("/nick "):
            new_nick = stripped[6:].strip()
            if new_nick:
                commands.append({"type": "nick", "args": new_nick})
        elif stripped.startswith("/msg "):
            parts = stripped[5:].strip().split(" ", 1)
            if len(parts) == 2:
                commands.append({"type": "msg", "target": parts[0], "text": parts[1]})
            elif len(parts) == 1:
                commands.append({"type": "msg", "target": parts[0], "text": ""})
        elif stripped.startswith("/join "):
            chan = stripped[6:].strip()
            if chan:
                commands.append({"type": "join", "args": chan})
        elif stripped.startswith("/part "):
            chan = stripped[6:].strip()
            if chan:
                commands.append({"type": "part", "args": chan})
        elif stripped.startswith("/me "):
            action = stripped[4:].strip()
            if action:
                commands.append({"type": "me", "args": action})
        else:
            remaining.append(line)

    remaining_text = "\n".join(remaining).strip()
    return commands, remaining_text

# ============================================================================
# MAIN LOOP
# ============================================================================

async def run_adapter(agents, channel, host, port):
    adapter_api.ensure_dirs(ADAPTER_NAME)
    batcher = MessageBatcher()

    connections = {}
    agent_list = list(agents)
    channel_listener = agent_list[0]

    tprint(f"[irc-adapter] Starting multi-nick IRC adapter")
    tprint(f"[irc-adapter]   Channel: {channel}")
    tprint(f"[irc-adapter]   Agents: {', '.join(agent_list)}")
    tprint(f"[irc-adapter]   Channel listener: {channel_listener}")
    tprint(f"[irc-adapter]   Routing: channel -> broadcast, PM -> agent")

    for agent_name in agent_list:
        nick = agents[agent_name]
        conn = IRCConnection(nick, channel, host, port, agent_name)
        connections[agent_name] = conn  # always register so reconnect loop can retry
        try:
            await conn.connect()
            await conn.join()
            _joined_channels.add(channel)
            tprint(f"[irc-adapter] {agent_name} connected as {nick}")
        except Exception as e:
            tprint(f"[irc-adapter] {agent_name} connect failed (will retry): {e}")
        await asyncio.sleep(0.5)

    # Join additional channels from agents' awareness configs.
    # The listener joins ALL awareness channels (it's the single source for
    # channel messages — section 1 handles routing via awareness cache).
    # Other agents only need their own channels joined for outbound sends.
    refresh_awareness_cache(agent_list)
    all_awareness_channels = set()
    for agent_name in agent_list:
        all_awareness_channels.update(get_awareness_channels(agent_name))
    all_awareness_channels.discard(channel)  # main channel already joined
    if channel_listener in connections:
        for ch in sorted(all_awareness_channels):
            await connections[channel_listener].join_if_needed(ch)
    # Other agents join their own awareness channels (for sending)
    for agent_name, conn in connections.items():
        if agent_name == channel_listener:
            continue
        for ch in get_awareness_channels(agent_name):
            if ch != channel:
                await conn.join_if_needed(ch)

    if not connections:
        tprint("[irc-adapter] ERROR: No connections. Exiting.")
        return

    tprint(f"[irc-adapter] All {len(connections)} agents online. Running.")

    _last_heartbeat = time.time()
    
    # Phase 7.1: Register adapter
    adapter_api.register_adapter(
        name=ADAPTER_NAME,
        capabilities=["send", "receive", "broadcast"],
        config={
            "type": "direct",
            "channel": channel,
            "agents": agent_list,
            "host": host,
            "port": port,
        },
    )

    while True:
        # ---- Reconnect dropped connections ----
        for name, conn in list(connections.items()):
            if not conn.connected:
                await conn.reconnect()

        # ---- 1. Main channel messages (from listener) → broadcast ----
        if channel_listener in connections and connections[channel_listener].connected:
            try:
                msgs = await connections[channel_listener].poll()
                for msg in msgs:
                    if msg["is_pm"]:
                        # PM to the listener agent
                        batcher.add(channel_listener, msg)
                    elif msg["target"] == channel:
                        # Main channel message → everyone except the sender
                        sender_lower = msg["sender"].lower()
                        for agent_name in connections:
                            agent_nick = connections[agent_name].nick.lower()
                            if agent_nick == sender_lower:
                                continue  # don't echo back to sender
                            batcher.add(agent_name, msg)
                    else:
                        # Non-main channel — deliver to all agents whose
                        # awareness includes this channel
                        target_ch = msg.get("target", "")
                        sender_lower = msg["sender"].lower()
                        for agent_name in connections:
                            agent_nick = connections[agent_name].nick.lower()
                            if agent_nick == sender_lower:
                                continue
                            if target_ch in get_awareness_channels(agent_name):
                                batcher.add(agent_name, msg)
            except Exception as e:
                tprint(f"[irc-adapter] Channel poll error: {e}")

        # ---- 2. PMs on non-listener connections ----
        # Section 1 handles ALL channel messages (main + awareness channels)
        # via the listener connection. Section 2 only handles PMs to
        # individual agent nicks and drains their poll buffers.
        for agent_name, conn in connections.items():
            if agent_name == channel_listener or not conn.connected:
                continue
            try:
                msgs = await conn.poll()
                for msg in msgs:
                    if msg["is_pm"]:
                        batcher.add(agent_name, msg)
                    # Channel messages are handled by section 1 via listener;
                    # discard duplicates from this agent's own connection.
            except Exception as e:
                tprint(f"[irc-adapter] {agent_name} poll error: {e}")

        # ---- 3. Flush batches → hub inbox ----
        for agent_name in batcher.ready_agents():
            msgs = batcher.flush(agent_name)
            if not msgs:
                continue

            # Coalesce consecutive messages from the same sender into one block.
            # When a long message is split across multiple IRC lines (400-char limit),
            # the agent should see it as one message, not N separate ones.
            parts = []
            senders = set()
            prev_sender = None
            prev_src = None
            current_lines = []
            for msg in msgs:
                senders.add(msg["sender"])
                src = "PM" if msg["is_pm"] else msg["target"]
                if msg["sender"] == prev_sender and src == prev_src:
                    # Same sender, same source — continuation of the same message
                    current_lines.append(msg["text"])
                else:
                    # New sender or source — flush previous block
                    if current_lines:
                        parts.append(f"[IRC {prev_src} from {prev_sender}]\n" + "\n".join(current_lines))
                    current_lines = [msg["text"]]
                    prev_sender = msg["sender"]
                    prev_src = src
            # Flush last block
            if current_lines:
                parts.append(f"[IRC {prev_src} from {prev_sender}]\n" + "\n".join(current_lines))
            combined = "\n\n".join(parts)

            # Determine room for gaze matching
            all_pm = all(m["is_pm"] for m in msgs)
            if all_pm and len(senders) == 1:
                room = f"pm:{list(senders)[0]}"
            else:
                # Use the actual IRC channel from the message, not the adapter default.
                # This allows gaze to match #meetingroom1, #standup, etc. correctly.
                room = msgs[0].get("target", channel)

            try:
                msg_id = adapter_api.write_to_adapter_inbox(
                    adapter_name=ADAPTER_NAME,
                    to=agent_name,
                    text=combined,
                    sender=list(senders)[0] if len(senders) == 1 else "multiple",
                    meta={
                        "room": room,
                        "channel": channel,
                        "senders": list(senders),
                        "batch_size": len(msgs),
                        "has_pm": any(m["is_pm"] for m in msgs),
                    },
                )
                tprint(f"[irc-adapter] {list(senders)} -> {agent_name} ({len(msgs)} msg, id: {msg_id[:8]})")
            except Exception as e:
                tprint(f"[irc-adapter] Inbox write error: {e}")

        # ---- 4. Hub outbox → IRC ----
        try:
            # Poll per-agent outboxes (new) + legacy outbox (backward compat)
            responses = []
            for agent_name_key in connections:
                per_agent = adapter_api.poll_adapter_outbox(ADAPTER_NAME, agent_name_key)
                responses.extend(per_agent)
            # Also check legacy outbox for backward compatibility
            legacy = adapter_api.poll_responses(ADAPTER_NAME)
            responses.extend(legacy)
            for resp in responses:
                agent_name = resp.get("from", "Sr")
                content_type = resp.get("content_type", "speech")
                raw_text = resp.get("text", "")
                text = clean_response(raw_text)
                meta = resp.get("meta", {})

                if text is None:
                    raw_alpha = "".join(c for c in raw_text if c.isalpha()).lower()
                    if raw_alpha in ("note", "noted"):
                        tprint(f"[irc-adapter] {agent_name} -> {content_type} suppressed (note)")
                    continue

                # Debug: log text length at each stage to catch truncation
                if len(raw_text) != len(text):
                    tprint(f"[irc-adapter] DEBUG: {agent_name} raw={len(raw_text)} clean={len(text)} (lost {len(raw_text)-len(text)} chars in clean_response)")

                conn = connections.get(agent_name)
                if conn and conn.connected:
                    # Determine target channel based on content_type
                    if content_type == "thoughts":
                        # Thoughts go to per-agent thought channel
                        # room key (new) takes precedence, fall back to channel (legacy)
                        thought_channel = resp.get("room", resp.get("channel", THOUGHT_CHANNELS.get(agent_name, f"#{agent_name.lower()}-thoughts")))
                        await conn.join_if_needed(thought_channel)
                        # No IRC command parsing for thoughts — send raw
                        await conn.send(text, target=thought_channel)
                        tprint(f"[irc-adapter] {agent_name} thoughts -> {thought_channel} ({len(text)} chars)")
                    else:
                        # Speech: parse for IRC slash commands, route normally
                        commands, remaining = parse_irc_commands(text)

                        # Debug: log if commands were extracted
                        if commands:
                            tprint(f"[irc-adapter] DEBUG: {agent_name} extracted {len(commands)} IRC commands: {[c['type'] for c in commands]}, remaining={len(remaining)} chars (was {len(text)})")

                        # Execute any IRC commands
                        for cmd in commands:
                            if cmd["type"] == "nick":
                                old_nick = conn.nick
                                await conn.change_nick(cmd["args"])
                                MIKEYV_NICKS.discard(old_nick.lower())
                                MIKEYV_NICKS.add(cmd["args"].lower())
                            elif cmd["type"] == "msg":
                                await conn.send(cmd["text"], target=cmd["target"])
                                tprint(f"[irc-adapter] {agent_name} PM -> {cmd['target']} ({len(cmd['text'])} chars)")
                            elif cmd["type"] == "join":
                                await conn.join(cmd["args"])
                                _joined_channels.add(cmd["args"])
                                tprint(f"[irc-adapter] {agent_name} joined {cmd['args']}")
                            elif cmd["type"] == "part":
                                conn.writer.write(f"PART {cmd['args']}\r\n".encode())
                                await conn.writer.drain()
                                _joined_channels.discard(cmd["args"])
                                tprint(f"[irc-adapter] {agent_name} left {cmd['args']}")
                            elif cmd["type"] == "me":
                                action_msg = f"\x01ACTION {cmd['args']}\x01"
                                await conn.send(action_msg)
                                tprint(f"[irc-adapter] {agent_name} * {cmd['args']}")

                        # Send remaining text as regular message
                        if remaining:
                            # Route based on room key from gaze (new convention)
                            gaze_room = resp.get("room")
                            if gaze_room and gaze_room.startswith("pm:"):
                                # PM room: "pm:nick" -> PRIVMSG to nick
                                pm_target = gaze_room[3:]
                                await conn.send(remaining, target=pm_target)
                                tprint(f"[irc-adapter] {agent_name} PM -> {pm_target} ({len(remaining)} chars)")
                            elif gaze_room and gaze_room.startswith("#"):
                                # Channel room: "#standup" -> channel message
                                await conn.send(remaining, target=gaze_room)
                                tprint(f"[irc-adapter] {agent_name} -> {gaze_room} ({len(remaining)} chars)")
                            else:
                                # Legacy fallback: use channel param or default channel
                                target_channel = resp.get("channel", channel)
                                # Legacy PM support
                                gaze_pm = resp.get("pm")
                                if gaze_pm:
                                    await conn.send(remaining, target=gaze_pm)
                                    tprint(f"[irc-adapter] {agent_name} PM -> {gaze_pm} ({len(remaining)} chars)")
                                else:
                                    await conn.send(remaining, target=target_channel)
                                    tprint(f"[irc-adapter] {agent_name} -> {target_channel} ({len(remaining)} chars)")
                else:
                    for fb in connections.values():
                        if fb.connected:
                            await fb.send(f"[{agent_name}] {text}")
                            break
        except Exception as e:
            tprint(f"[irc-adapter] Outbox error: {e}")
            import traceback
            traceback.print_exc()

        # Phase 7.1: Periodic heartbeat + awareness channel sync
        _now = time.time()
        if _now - _last_heartbeat >= 30:
            adapter_api.update_heartbeat(ADAPTER_NAME)
            # Re-check awareness for new channels to join
            refresh_awareness_cache(list(connections.keys()))
            all_aw_ch = set()
            for an in connections:
                all_aw_ch.update(get_awareness_channels(an))
            all_aw_ch.discard(channel)
            if channel_listener in connections and connections[channel_listener].connected:
                for ch in sorted(all_aw_ch):
                    await connections[channel_listener].join_if_needed(ch)
            for agent_name, conn in connections.items():
                if agent_name == channel_listener or not conn.connected:
                    continue
                for ch in get_awareness_channels(agent_name):
                    if ch != channel:
                        await conn.join_if_needed(ch)
            _last_heartbeat = _now

        await asyncio.sleep(OUTBOX_POLL_INTERVAL)


def main():
    parser = argparse.ArgumentParser(description="MikeyV IRC Adapter")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--agents", default=None, help="Comma-separated subset (default: all)")
    args = parser.parse_args()

    if args.agents:
        names = [a.strip() for a in args.agents.split(",")]
        agents = {a: AGENT_NICKS[a] for a in names if a in AGENT_NICKS}
    else:
        agents = dict(AGENT_NICKS)

    try:
        asyncio.run(run_adapter(agents, args.channel, args.host, args.port))
    except KeyboardInterrupt:
        print("\n[irc-adapter] Shutting down.")


if __name__ == "__main__":
    main()
