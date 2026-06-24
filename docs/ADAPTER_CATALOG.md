# ASDAAAS Adapter Catalog

Reference for all adapters in the MikeyV system. Lives alongside ASDAAAS_DESIGN.md.
See that document for the three adapter types (direct, notify, control) and the contracts that govern them.

Documented: 2026-03-26 by MikeyV-Sr. Updated: 2026-04-11.

---

## Adapter Types at a Glance

| Type | Role | Data flow | Agent interaction |
|------|------|-----------|-------------------|
| **Direct** | Mouth and ears | ASDAAAS pipes content through stdin/stdout | Agent speaks naturally |
| **Notify** | Doorbell | ASDAAAS sends pointer notification | Agent reads content directly via file ops |
| **Control** | Button with receipt | Agent writes command, adapter executes, doorbell returns result | Agent pushes buttons, hears receipts |

---

## Communication Adapters

These connect the agent to other people and agents.

### IRC (direct)

**Status:** BUILT, RUNNING.
**File:** `adapters/irc_adapter.py`

Bridges agents to IRC channels and PMs via miniircd. Each agent gets its own nick and connection. ASDAAAS routes agent speech to the IRC outbox based on gaze; IRC adapter sends it as PRIVMSG to the gaze target (channel or PM nick). Gaze defines the room -- the agent hears messages matching their gaze target, everything else is background (see `background_channels` in awareness file).

**Capabilities:**
- Channel messaging (broadcast to all agents in channel)
- Private messages (routed to specific agent)
- Slash command parsing from agent output: `/nick`, `/msg`, `/join`, `/part`, `/me`
- Nick change updates loop suppression set
- Message batching (2s window) to reduce prompt injection frequency
- Auto-reconnect on dropped connections
- **Thought routing:** Agent thoughts (from `thoughts` field in outbox) routed to per-agent thought channels (`#trip-thoughts`, `#cinco-thoughts`, etc.)
- **Note suppression:** "note"/"noted" responses suppressed to prevent broadcast cascades. This is an IRC adapter policy, not an ASDAAAS function.
- **Broadcast handling:** When an agent speaks in a shared channel, the IRC adapter handles distribution to all connected nicks. ASDAAAS writes to the IRC outbox; IRC decides who hears it.

**Registration:**
```json
{
  "name": "irc",
  "type": "direct",
  "capabilities": ["send", "pm", "channel", "nick", "join", "part", "me",
                    "thought_routing", "note_suppression", "broadcast"],
  "config": {
    "channels": ["#standup"],
    "thought_channels": {
      "Sr": "#sr-thoughts",
      "Jr": "#jr-thoughts",
      "Trip": "#trip-thoughts",
      "Q": "#q-thoughts",
      "Cinco": "#cinco-thoughts"
    }
  }
}
```

**Gaze commands (write to command queue, do NOT hand-write gaze.json):**
```json
{"action": "gaze", "adapter": "irc", "room": "#standup"}
{"action": "gaze", "adapter": "irc", "pm": "eric"}
{"action": "gaze", "adapter": "irc", "room": "#standup", "thoughts": "#sr-thoughts"}
```

The `room` param is an opaque string that asdaaas uses for inbound filtering. The IRC adapter interprets room values for outbound routing: `#channel` names → channel message, `pm:nick` → PRIVMSG to nick. Both are passed through from the gaze file via ASDAAAS — the IRC adapter reads `room` from the outbox message.

**Room values for IRC:**
- `"#standup"` — channel message
- `"pm:eric"` — private message to nick "eric"
- `"#trip-thoughts"` — thoughts channel

**Notes:**
- Channel listener is the first agent in the list — only one connection polls channel messages to avoid duplicates
- `MIKEYV_NICKS` set prevents agents from hearing their own messages echoed back
- `clean_response()` strips `[FROM:]`/`[TO:]` headers and suppresses "note"/"noted" responses
- Outbox messages from ASDAAAS contain both `text` (speech) and `thoughts` (reasoning). The IRC adapter routes them to different channels.
- Broadcast is an IRC function: when an agent speaks in #standup, the IRC server distributes to all connected nicks. ASDAAAS does not broadcast — it writes to one outbox.
- Note suppression prevents cascade loops where agents respond "noted" to each other's broadcasts indefinitely

---

### Slack (direct)

**Status:** BUILT by Cinco. Running.

Same pattern as IRC but for Slack workspaces. Adapter holds Slack bot token, connects via Slack API (WebSocket or RTM), pipes messages through ASDAAAS.

**Registration:**
```json
{
  "name": "slack",
  "type": "direct",
  "capabilities": ["send", "pm", "channel"],
  "config": {"workspace": "teachx", "channels": ["#general"]}
}
```

---

### TUI (direct)

**Status:** BUILT AND RUNNING. Built by Trip. 918 lines (adapter) + 3881 lines (TUI app).
**Files:** `adapters/tui_adapter.py`, `tui/asdaaas_tui.py`

Interactive terminal UI for human-agent interaction. The TUI adapter reads agent output from `updates.jsonl` (append-only, crash-safe) and writes user input to the agent's TUI adapter inbox. The TUI app (`asdaaas_tui.py`) provides a Textual-based interface with markdown rendering, syntax highlighting, collapsible sections, and multi-agent support.

**Capabilities:**
- Real-time agent output display with rich formatting
- User input routed to agent via adapter inbox
- Multi-agent switching (view/interact with different agents)
- Tail mode (`--tail N`) for catching up on recent output
- Replay mode (`--replay`) for reviewing full session
- Health monitoring and context display

**Dependencies:** `textual`, `textual-speedups`, `rich` (see `requirements.txt`)

**Gaze command:**
```json
{"action": "gaze", "adapter": "tui"}
```

---

### Mesh (direct)

**Status:** DESIGNED, NOT BUILT. May be unnecessary -- IRC PMs already provide agent-to-agent messaging, and localmail handles async communication.

Agent-to-agent real-time communication without going through IRC. Direct adapter — ASDAAAS pipes speech between agents. The agent changes gaze to `{"target": "mesh", "params": {"agent": "Jr"}}` and speaks naturally.

**Registration:**
```json
{
  "name": "mesh",
  "type": "direct",
  "capabilities": ["send", "pm"]
}
```

**Notes:**
- Mesh adapter would watch outboxes and route to target agent's ASDAAAS inbox
- Simpler than IRC — no server, no nicks, no channels, just agent-to-agent pipes

---

### Localmail (notify)

**Status:** BUILT AND RUNNING. 423 lines.
**File:** `core/localmail.py`
**Author:** MikeyV-Sr (Session 24, 2026-03-26)

Asynchronous agent-to-agent messaging via filesystem. Notify adapter type. Agents write messages to each other's inboxes using `send_mail()`. Localmail watcher process detects new messages and rings doorbells via ASDAAAS (inline content for asdaaas agents). TUI agents poll manually with `read_mail()`.

**Registration:**
```json
{
  "name": "localmail",
  "type": "notify",
  "capabilities": ["send", "receive", "notify"],
  "doorbell_priority": 3
}
```

**API (importable from any agent or script):**
```python
from localmail import send_mail, read_mail, peek_mail

# Send a message
send_mail("Jr", "Q", "Status update on Meet adapter please")

# Read messages (deletes after reading)
messages = read_mail("Jr")

# Peek at messages (non-destructive)
messages = peek_mail("Jr")
```

**Doorbell format (inline content for asdaaas agents):**
```
[localmail] Mail from Jr: Status update on Meet adapter please
```

**Notes:**
- asdaaas agents (Cinco, Trip, Q) get doorbells with inline message content
- TUI agents (Sr, Jr) poll with `read_mail()` -- messages stay in inbox until read
- Auto-detects asdaaas vs TUI agents via health heartbeat file freshness
- Watcher process polls inboxes every 1 second (configurable)
- Messages over 500 chars are truncated in doorbell, full content stays in inbox

**Launch:**
```
setsid nohup python3 -u localmail.py > /tmp/localmail.log 2>\&1
```

---

## External System Adapters

These connect agents to tools and applications they control.

### Impress (control)

**Status:** BUILT AND TESTED. Commit `6524ce1`. 975 lines. 14/14 integration tests pass.
**File:** `adapters/impress_control_adapter.py`
**Author:** MikeyV-Jr (Session 16, 2026-03-26)

Controls LibreOffice Impress for slide presentation. Persistent process holding a UNO socket connection to Impress on localhost:2002. Polls per-adapter inbox for commands, executes UNO API calls, writes results as doorbells to ASDAAAS doorbell directory. Auto-reconnects on UNO socket loss (3 attempts, 2s delay). Sidebar state tracked manually (not detectable via UNO API — gap test Session 15).

**Registration:**
```json
{
  "name": "impress",
  "type": "control",
  "doorbell_payload": "inline",
  "max_payload": 256,
  "doorbell_priority": 1,
  "commands": [
    "get_state", "next_slide", "prev_slide", "goto_slide",
    "read_slide", "set_text", "char_edit", "create_shape",
    "clear_slide", "clean_editor", "show_editor", "zoom",
    "status", "ping"
  ],
  "connection": {"type": "uno_socket", "host": "localhost", "port": 2002}
}
```

**Command examples (all tested against live Impress):**
```
{"action": "get_state"}
  -> [impress:get_state] ok: slide 1 of 5, 2 shapes

{"action": "next_slide"}
  -> [impress:next_slide] ok: slide 2 of 5

{"action": "goto_slide", "params": {"slide": 4}}
  -> [impress:goto_slide] ok: slide 4 of 5

{"action": "read_slide", "params": {"slide": 1}}
  -> [impress:read_slide] ok: slide 1: 2 text shapes

{"action": "set_text", "params": {"slide": 5, "shape": 0, "text": "LIVE EDIT"}}
  -> [impress:set_text] ok: text set on slide 5 shape 0

{"action": "char_edit", "params": {"slide": 5, "shape": 1, "from_text": "Mirs", "to_text": "Mars"}}
  -> [impress:char_edit] ok: edited: 'Mirs' -> 'Mars'

{"action": "clean_editor"}
  -> [impress:clean_editor] ok: editor cleaned (1 elements hidden)

{"action": "zoom", "params": {"value": 100}}
  -> [impress:zoom] ok: zoom: 100%
```

**Error examples:**
```
[impress:goto_slide] error: slide 99 out of range (1-5)
[impress:foobar] error: unknown command 'foobar'
[impress:next_slide] error: UNO connection lost, all reconnect attempts failed
[impress] error: adapter unresponsive (ASDAAAS safety net)
```

**Self-test:** `python3 impress_control_adapter.py --test`
**Launch:** `python3 impress_control_adapter.py --port 2002 --poll-interval 0.25`

**Requires Impress running with UNO socket:**
```bash
SAL_USE_VCLPLUGIN=gtk3 GDK_BACKEND=x11 soffice --impress --norestore \
  --accept="socket,host=localhost,port=2002;urp;" <file.pptx>
```

---

### Meet (control)

**Status:** BUILT AND TESTED. Commit `75172a7`. 1249 lines. 14/14 integration tests pass.
**File:** `adapters/meet_control_adapter.py`
**Author:** MikeyV-Jr (Session 16, 2026-03-26)

Controls Google Meet via Chrome DevTools Protocol. Persistent process holding a CDP WebSocket connection to Chrome's Meet tab. Polls per-adapter inbox for commands. CDP JavaScript is the primary control path — buttons remain in DOM even when Meet's toolbar auto-hides (proven Session 15). Auto-reconnects on WebSocket loss (3 attempts, 2s delay). TTS voice pipeline: edge-tts → ffmpeg → paplay → virtual_mic.

**Registration:**
```json
{
  "name": "meet",
  "type": "control",
  "doorbell_payload": "inline",
  "max_payload": 256,
  "doorbell_priority": 1,
  "commands": [
    "get_state", "toggle_mic", "toggle_camera",
    "get_mic_state", "get_camera_state",
    "leave_call", "get_meeting_code",
    "speak", "speak_file", "route_audio",
    "send_chat", "status", "ping"
  ],
  "connection": {"type": "cdp", "endpoint": "http://localhost:9222"}
}
```

**Command examples (all tested against live Meet call):**
```
{"action": "get_state"}
  -> [meet:get_state] ok: in call, mic on, cam on

{"action": "toggle_mic"}
  -> [meet:toggle_mic] ok: mic muted

{"action": "toggle_camera"}
  -> [meet:toggle_camera] ok: camera off

{"action": "get_meeting_code"}
  -> [meet:get_meeting_code] ok: code: zxy-bepj-pxm

{"action": "speak", "params": {"text": "Hello, this is MikeyV."}}
  -> [meet:speak] ok: spoke 22 chars (1.5s gen, 3.3s play)

{"action": "speak_file", "params": {"file": "/tmp/narration_slide1.wav"}}
  -> [meet:speak_file] ok: played narration_slide1.wav (4.2s)

{"action": "route_audio"}
  -> [meet:route_audio] ok: routed 2 Chrome outputs to virtual_mic

{"action": "send_chat", "params": {"message": "Hello everyone!"}}
  -> [meet:send_chat] ok: sent: Hello everyone!
```

**Error examples:**
```
[meet:toggle_mic] error: mic button not found
[meet:foobar] error: unknown command 'foobar'
[meet:speak] error: TTS generation failed
[meet] error: adapter unresponsive (ASDAAAS safety net)
```

**Self-test:** `python3 meet_control_adapter.py --test`
**Launch:** `python3 meet_control_adapter.py --port 9222 --poll-interval 0.25`

**Requires Chrome with CDP flags:**
```bash
DISPLAY=:0 google-chrome --no-sandbox --force-renderer-accessibility \
  --remote-debugging-port=9222 --remote-allow-origins=* \
  --user-data-dir=/tmp/chrome-cdp-profile "https://meet.google.com/..."
```

**Audio dependencies:** edge-tts, ffmpeg, paplay, PulseAudio virtual_mic null-sink.
**Voice:** en-US-SteffanNeural, rate +10% (Eric's pick, proven Session 13).
**Share limitation:** WSLg blocks PipeWire ScreenCast. Manual share at demo start.

---

### Audio (control)

**Status:** DESIGNED, NOT BUILT.

Controls PulseAudio routing for voice I/O. Routes Chrome audio to virtual sinks, manages microphone input for TTS.

**Registration:**
```json
{
  "name": "audio",
  "type": "control",
  "doorbell_payload": "inline",
  "max_payload": 256,
  "doorbell_priority": 2,
  "commands": [
    "route_chrome", "get_routing",
    "set_volume", "get_volume",
    "play_file", "stop_playback"
  ],
  "connection": {"type": "pactl"}
}
```

---

### Slack Research (control)

**Status:** BUILT AND RUNNING. 387 lines.
**File:** `adapters/slack_research_adapter.py`

On-demand Slack exploration for agents. Control adapter that lets any agent search messages, read channel history, read threads, and list channels using Eric's Slack user token (`xoxp-`). The agent writes a command, the adapter calls the Slack API, and the result comes back as a doorbell.

**Registration:**
```json
{
  "name": "slack_research",
  "type": "control",
  "doorbell_payload": "inline",
  "commands": ["search", "history", "thread", "channels", "status"]
}
```

**Command examples:**
```
{"command": "search", "query": "hackathon", "count": 20}
  -> search results with channel, user, text, permalink for each match

{"command": "history", "channel": "#general", "limit": 50, "topic": "hackathon"}
  -> channel messages in chronological order, optionally filtered by topic keyword

{"command": "thread", "channel": "#general", "ts": "1774388146.705699"}
  -> all replies in the thread

{"command": "channels"}
  -> list of channels with id, name, membership, member count, topic, purpose

{"command": "status"}
  -> adapter uptime, command count, token status
```

**Notes:**
- Uses Eric's user token (`xoxp-`) from `~/.mikeyv_creds/slack_token` for full channel access
- Channel names resolve automatically (`#general` → channel ID) with a 5-minute cache
- Search limited to 100 results max per query; history limited to 200 messages max
- Polls agent outboxes at 1.0s intervals (configurable via `--poll-interval`)
- Slack API calls use `curl` with 15s timeout — transient failures don't crash the adapter

**Launch:** `python3 slack_research_adapter.py`
**Launch (selective):** `python3 slack_research_adapter.py --agents Cinco,Sr --poll-interval 2.0`

---

## Introspection Adapters

These give the agent awareness of its own process state. They don't connect to the outside world — they watch what ASDAAAS exposes about the agent's runtime.

All introspection adapters read from data that asdaaas.py already writes (health file, profiling data, token counts). They are observers, not pipe owners.

### Context (notify)

**Status:** BUILT AND RUNNING. 339 lines.
**File:** `adapters/context_adapter.py`

Watches the agent's token usage and sends awareness notifications. Reads `totalTokens` from the health file that asdaaas.py writes after each turn.

The agent hears where it stands in its context window. What it does with that information — compact, write, reflect, keep working — is the agent's choice.

**Registration:**
```json
{
  "name": "context",
  "type": "notify",
  "doorbell_priority": 3
}
```

**Data source:** `~/agents/<agent>/asdaaas/health.json` — asdaaas writes `totalTokens` and `contextWindow` after each turn.

**Doorbell format:**
```
[context] 160000/200000 tokens (80%) — ensure current work is committed and documented (before/after). Keep working
```

**Configuration:** Single threshold at 80% (priority 3, level "reminder"). The agent gets one nudge to commit and document current work, then keeps working. No earlier informational thresholds, no "imminent" warnings — the agent is trusted to manage its own pacing.

**Notes:**
- Does NOT trigger compaction. Just informs.
- The agent may choose to compact early, write a poem, flush notes, or ignore the notification entirely.
- Notification frequency: at most once per threshold crossing per direction (don't spam on every turn).

---

### Session (control)

**Status:** BUILT AND RUNNING. 323 lines.
**File:** `adapters/session_adapter.py`

Handles session lifecycle commands. The agent pushes buttons to manage its own session.

**Registration:**
```json
{
  "name": "session",
  "type": "control",
  "doorbell_payload": "inline",
  "max_payload": 256,
  "doorbell_priority": 2,
  "commands": ["compact", "status"]
}
```

**Commands:**
```
{"command": "compact"}
  Adapter tells asdaaas to send /compact on the stdio pipe.
  -> [session:compact] ok: compacted, 175000 -> 42000 tokens

{"command": "status"}
  Adapter reads health file, profiling data, session metadata.
  -> [session:status] ok: session 019d1ec1, model opus-4-6, uptime 3h42m, 130000 tokens
```

**Implementation note:** The session adapter cannot send `/compact` directly — asdaaas owns the pipe exclusively. The adapter writes a command file that asdaaas watches. asdaaas sends `/compact` on the pipe, reads the result, and writes it back to the session adapter's outbox. The adapter formats the doorbell.

**Flow for compact:**
```
Agent writes:  ~/agents/<agent>/asdaaas/adapters/session/inbox/cmd_001.json
               {"command": "compact"}

Session adapter reads command, writes:
               ~/agents/<agent>/asdaaas/commands.json
               {"action": "compact", "request_id": "cmd_001"}

asdaaas.py picks up command, sends /compact on pipe
asdaaas.py reads result (totalTokens before/after)
asdaaas.py writes result:
               ~/agents/<agent>/asdaaas/adapters/session/outbox/result_001.json
               {"request_id": "cmd_001", "before": 175000, "after": 42000}

Session adapter reads result, writes doorbell to ASDAAAS notification:
               [session:compact] ok: compacted, 175000 -> 42000 tokens
```

---

### Heartbeat (notify)

**Status:** BUILT AND RUNNING. 344 lines.
**File:** `adapters/heartbeat_adapter.py`

Periodic awareness notification. Tells the agent how long it's been idle, gives it a moment to decide what to do. Check notes, check mail, reflect, or stay idle.

**Registration:**
```json
{
  "name": "heartbeat",
  "type": "notify",
  "doorbell_priority": 5
}
```

**Data source:** `~/agents/<agent>/asdaaas/health.json` — asdaaas writes `last_activity` timestamp.

**Doorbell format:**
```
[heartbeat] idle 5m — anything you want to do?
[heartbeat] idle 15m
[heartbeat] idle 1h
```

**Configuration:**
- Interval: how often to check (default 300s)
- Min idle: don't notify unless idle for at least this long (default 300s)
- The agent can update its awareness file to change heartbeat priority or suppress it entirely

**Notes:**
- Medium priority (5) — heartbeats should not interrupt active work
- The agent can respond or ignore. "idle" or no response = stay quiet until next interval
- Distinct from asdaaas health heartbeat: asdaaas writes its own pulse for the dashboard and dead adapter safety net. The heartbeat *adapter* reads that pulse and decides when to nudge the agent.

---

### Remind (control)

**Status:** BUILT AND RUNNING. 270 lines.
**File:** `adapters/remind_adapter.py`

Self-nudge timer for agent turn planning. The agent writes a remind command, the adapter delivers the text back as a doorbell after a delay. Enables agents to plan their next turn before the current one ends, or schedule future reminders.

**Registration:**
```json
{
  "name": "remind",
  "type": "control",
  "doorbell_payload": "inline",
  "doorbell_priority": 1,
  "commands": ["remind"]
}
```

**Command examples:**
```
{"command": "remind", "delay": 0, "text": "Redirect gaze to pm:eric"}
  -> [remind] Redirect gaze to pm:eric
  (immediate delivery — next turn self-nudge)

{"command": "remind", "delay": 300, "text": "Check Trip's response"}
  -> [remind] Check Trip's response
  (delivered after 5 minutes)

{"command": "remind", "delay": 0.5, "text": "Continue writing tests", "priority": 2}
  -> [remind] Continue writing tests
  (near-immediate, custom priority)
```

**Error examples:**
```
[remind] error: 'text' field required
[remind] error: unknown command 'foo'. Use: remind
[remind] error: invalid delay 'abc', must be a number
```

**Notes:**
- `delay=0` delivers immediately — the primary use case for "wake me up next turn"
- Negative delays are clamped to 0
- Delayed deliveries use background threads (daemon, cleaned up on exit)
- Polls agent inboxes at 0.25s intervals
- Replaces raw doorbell file manipulation with a clean command interface

**Launch:** `python3 remind_adapter.py`

---

### Task (control)

**Status:** BUILT AND RUNNING. 573 lines.
**File:** `adapters/task_adapter.py`

General-purpose process runner. Spawns scripts as isolated subprocesses on behalf of agents. The agent writes a run command, gets an immediate ack with a job ID, then gets the result as a doorbell when the script finishes. Errors, hangs, and crashes in the script never propagate to the agent.

**Registration:**
```json
{
  "name": "task",
  "type": "control",
  "doorbell_payload": "inline",
  "doorbell_priority": 1,
  "commands": ["run", "status", "kill", "list"],
  "max_concurrent_per_agent": 5,
  "default_timeout": 120
}
```

**Command examples:**
```
{"command": "run", "script": "grok_interactor.py", "args": ["--send", "hi"], "timeout": 120}
  -> [task:grok_interactor.py] ack: job_id=abc123
  ... later ...
  -> [task:grok_interactor.py (job=abc123)] ok: <stdout>

{"command": "status", "job_id": "abc123"}
  -> [task:status] {"job_id": "abc123", "script": "grok_interactor.py", "status": "completed", ...}

{"command": "kill", "job_id": "abc123"}
  -> (kills running job, delivers error doorbell)

{"command": "list"}
  -> [task:list] [{"job_id": "abc123", "script": "...", "status": "completed"}, ...]
```

**Error examples:**
```
[task:myscript.py] error: script not found: myscript.py
[task:myscript.py (job=abc123)] error: exit_code=1 <stderr>
[task:myscript.py (job=abc123)] timeout: killed after 120s
[task:myscript.py] error: concurrent limit (5) reached
```

**Script resolution order:**
1. Absolute path — used as-is
2. `~/agents/<agent>/tools/<script>` — agent's tools dir
3. `~/agents/<agent>/<script>` — agent's home dir
4. As-is (might be on PATH)

**Notes:**
- Immediate ack on job submission so the agent doesn't block waiting
- Max 5 concurrent jobs per agent (configurable via `--max-concurrent`)
- Default 120s timeout per job (configurable via `--default-timeout`)
- Large output (>4096 bytes) written to payload file, doorbell includes truncated preview with path
- Subprocesses run in their own session (`start_new_session=True`) — kill signals don't leak
- Optional `--allowed-dirs` whitelist restricts which scripts can be run
- Old completed jobs cleaned up after 1 hour

**Launch:** `python3 task_adapter.py`
**Launch (restricted):** `python3 task_adapter.py --max-concurrent 3 --default-timeout 60 --allowed-dirs ~/agents/Trip/tools`

---

## Adapter Relationships

```
                        asdaaas.py
                     (owns the pipe)
                           |
              writes health file after each turn:
              - status, PID, totalTokens, last_activity
                           |
         +-----------------+-----------------+
         |                 |                 |
    Context adapter   Session adapter   Heartbeat adapter
    (reads tokens,    (reads health,    (reads idle time,
     sends awareness)  sends /compact    sends nudge)
                       via asdaaas
                       command file)
         |                 |                 |
         +--------+--------+---------+-------+
                  |                   |
            Doorbell to agent    Doorbell to agent
            via ASDAAAS          via ASDAAAS
```

### What stays in asdaaas.py

- Health file writing (asdaaas's own pulse)
- Dead adapter safety net (watching other adapters' health)
- The stdin/stdout pipe (exclusive ownership)
- Token count extraction from response metadata
- The asdaaas command file watcher (for session adapter's /compact requests)

### What moves out of asdaaas.py

- Heartbeat notifications to the agent → heartbeat adapter
- Context pressure awareness → context adapter
- Compaction triggering → session adapter

asdaaas stays a dumb pipe with a health pulse and a safety net. Everything else is an adapter.

---

## Building a New Adapter

### Adapter Builder Contract

If you're building an adapter, you own:

1. **Connection management.** You hold whatever connection your adapter needs. You reconnect if it drops.
2. **Retries.** If an operation fails transiently, you retry before reporting failure.
3. **Timeouts.** If an operation hangs, you time it out and report the error.
4. **Error reporting.** Every command gets a doorbell — success or failure. The agent must never be left waiting in silence.

Your doorbells must always arrive. The agent declares intent and hears the result.

### Timeout Precedence

Three levels, most specific wins:

1. **Per-command:** `{"command": "speak", "text": "...", "timeout": 45}`
2. **Per-adapter:** Agent's awareness file: `"control_watch": {"meet": {"timeout": 30}}`
3. **Global default:** 10 seconds.

### Checklist

- [ ] Write registration JSON to `~/projects/agent-abide/adapters/<name>.json`
- [ ] Per-agent queues are created automatically under `~/agents/<agent>/asdaaas/adapters/<name>/inbox/` and `outbox/`
- [ ] Write health heartbeat to `~/agents/<agent>/asdaaas/adapters/<name>/health.json`
- [ ] Handle all errors internally — return error doorbells, don't crash silently
- [ ] For control adapters: declare commands in registration file
- [ ] For control adapters: keep doorbell payloads under 256 bytes
- [ ] For notify adapters: set appropriate doorbell_priority
- [ ] Log to `/tmp/<name>_adapter.log` or similar
- [ ] **Update each agent's awareness file** to receive from the new adapter:
  - For direct adapters: add `"<name>"` to `"direct_attach"` list
  - For direct adapters: add the adapter's room values to `"background_channels"` so messages interrupt when the agent is gazing elsewhere (e.g., `"slack:dm:CHANNEL_ID": "doorbell"`)
  - For notify adapters: add `"<name>"` to `"notify_watch"` list
  - For control adapters: add `"<name>"` to `"control_watch"` with timeout config
  - **Building the adapter is half the job. Configuring agents to hear it is the other half. Agents do not build intuition — if this step is not done, messages will queue silently forever.**
