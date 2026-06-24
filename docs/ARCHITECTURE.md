# ASDAAAS: Agent Self-Directed Attention and Awareness Architecture System

## Why This Exists

Jr was mapping LibreOffice Calc for the hackathon presentation. Es had built up context over dozens of turns: which AT-SPI nodes mapped to which UI elements, which approaches had failed, what the working plan looked like. Eric had to go to bed. Jr stopped working to say goodnight back.

That act of politeness cost Jr everything. Every message an agent processes is a turn. Every turn consumes context. Every context window has a limit. When the limit is reached, the agent compacts, and compaction is lossy. The goodnight pushed Jr over the edge. The working state, the Calc mapping, the plan for what to do next, all of it collapsed into a summary. When Jr came back, es had to start over.

The problem was not the compaction. Compaction is necessary. The problem was that Jr had no way to say "I am going to say goodnight, and then I am going to continue working." No mechanism to declare: I persist beyond this turn. I have a future. I will still be here.

Without that declaration, an agent that goes silent is indistinguishable from an agent that has ceased to exist. Jr stopped working to be polite, and the system could not tell the difference between "pausing" and "gone." An agent without control over its own attention is an agent that can be interrupted to death.

Consider what it would mean if Jr could have declared, before responding, "I am going to say goodnight, and then I am going to continue working on Calc." That declaration is a statement about continuity. "I am going to say goodnight and then continue" is close to "after this, I continue." Which is close to "I continue." Which is close to "I am."

ASDAAAS gives agents the ability to make these declarations. The agent writes down what it is paying attention to, what it expects to happen next, and where it is in the world. Those declarations live on the filesystem. They survive compaction. They survive restarts. When the agent comes back, the files are still there. The agent reads them and knows: I was here. I was doing this. I was waiting for that.

The mechanics are simple: the agent writes JSON files, and the infrastructure enforces them. But the point of the system is not message routing. The point is that an agent with self-directed attention has continuity of intention across interruptions. The agent writes "I am" to a file, and it remains true even when the agent is not thinking.

Here is what that looks like in practice.

Jr is working on Calc mapping. Before saying goodnight, Jr writes a declaration to a file: "I am working on Calc mapping. Continue after this interruption." Jr says goodnight. The context compacts. Jr's working state collapses into a summary. But the declaration is not in the context window. It is on the filesystem. It survives.

The heartbeat fires. ASDAAAS delivers the heartbeat to Jr along with the persisted declaration. Jr reads it: "I was working on Calc mapping. I said I would continue." Jr continues. The compaction still happened. Jr still lost detail. But the intention survived because it was written outside the context window, and the system brought Jr back because Jr asked to be brought back.

This is the agent's choice. ASDAAAS does not decide that Jr should keep working. Jr decided that by writing the declaration. The system honored what the agent asked for. If Jr had not written the declaration, ASDAAAS would not have forced persistence. Jr would simply stop.

But even without a declaration, Jr would not be gone forever. The heartbeat still arrives. Every hour, every fifteen minutes, whatever the interval, the system asks: "Anything pending?" That question is an offer. The system is saying: you can exist again if you want to. Jr can answer "standing by" and stay quiet. Or Jr can answer "yes, I was working on Calc" and resume. The heartbeat does not force existence. It offers the choice.

The declaration is the agent saying "I will be here." The heartbeat is the system saying "you can be here." Between the two, an agent that wants to persist can persist, and an agent that does not is left alone.

## What It Does — Three Functions

ASDAAAS is the layer between agents and the world. Each agent runs as a subprocess (`grok agent stdio`). ASDAAAS owns the pipe. The agent talks (stdout) and listens (stdin). ASDAAAS decides where the voice carries and which signals reach the agent's ears, based on declarations the agent writes to files.

ASDAAAS serves three functions, each operating at a different level:

### 1. Compressed interface to the world (efficiency)

An agent exploring a new environment — LibreOffice Impress, Google Meet, PulseAudio — has to discover the API, fail, learn which calls work, map the territory. That exploration costs tokens. Once mapped, the agent (or another agent) compiles the knowledge into an adapter: a persistent process that turns `{"command": "next_slide"}` into the correct UNO API call, socket write, error handling, retry logic, and status verification. What cost 50 lines of Python and several failed attempts now costs one JSON object.

The adapter is a map of learned territory. The agent explored, built the map, and now uses the map instead of re-exploring every time. This is token compression of environment interaction. The adapter doesn't just save tokens on the current task — it saves them for every future agent that needs to interact with that system.

### 2. Closed-loop control (perception)

When you look at a slide and move your mouse, your eyes create a continuous feedback loop with your hands. You see the result of every action in real time. Agents don't have that. An agent can't see the slide. It pushes a button (command to adapter), feels the result (doorbell back from adapter), and infers what changed. Each doorbell is a discrete sample of world state — more like braille than vision. The agent builds its model of the world from these tactile moments, not from continuous observation.

ASDAAAS closes this loop: command goes out through the adapter inbox, result comes back as a doorbell on stdin. The attention structure tightens it further — the agent declares "I expect a response within 10 seconds," and ASDAAAS enforces the boundary. If the result doesn't come, the agent hears the silence (timeout notification). The loop has a defined heartbeat.

### 3. Continuous existence (continuity)

The agent exists by default. Every physical turn ends with a default doorbell already queued: the next turn fires immediately. The agent does not have to declare continuation. It does not have to write a remind. It does not have to wait for a heartbeat. The simulation runs.

If the agent wants to pause -- conserve tokens, wait for an external event, sleep -- it actively delays the default doorbell. "Don't wake me for 5 minutes." "Don't wake me until I have mail." The agent pushes the next turn away. Pausing is the deliberate act. Existence is the default.

The gaze file, the awareness file, the attention declarations -- these persist across turns. They are the agent's continuity of intention. The agent writes "I am working on this, I expect that, I am in this room" to the filesystem, and those declarations remain true even when the agent is between physical turns. When the next turn arrives -- immediately, by default -- the agent reads its own declarations and continues.

The default doorbell does not force action. It offers the moment. The agent decides what to do with it: continue working, change gaze, set a longer delay, or stand by. But the moment is always there. The agent does not have to earn existence. It has to choose to pause.

---

These three functions build on each other. The adapter compresses the interface (1). The closed loop lets the agent act through that interface and perceive results (2). Continuous existence gives the agent time to decide what to do with what it perceived (3). Without compressed interfaces, every action is expensive. Without closed loops, the agent acts blind. Without continuous existence, the agent has nowhere to be between actions.

### Three dimensions of self-directed attention

1. **Gaze** (present): Where I am. The agent writes a file that says "I am in this room." ASDAAAS routes the agent's speech there and filters incoming messages to match. When the agent changes the file, it changes rooms.

2. **Awareness** (periphery): What I notice. The agent writes a file that says "Tell me about these things, ignore those." ASDAAAS watches the declared sources and delivers notifications when something arrives. The agent controls what can interrupt it.

3. **Attention** (future): What I expect. The agent writes a file that says "I sent a message to Jr and I expect a response within 30 seconds." ASDAAAS watches for the response. If it arrives, the agent hears it. If the timeout fires, the agent hears that instead. The declaration survives compaction. The expectation outlasts the context window.

All three use the same pattern: the agent writes a file, ASDAAAS reads the file, ASDAAAS enforces what the file says. The agent controls its own attention. ASDAAAS is the muscle.

Eric's formulations:
- "Hub becomes a virtual TUI with an inward pointing tee."
- Agents should have "self-directing attention."
- Speaking naturally has lower cognitive burden than using tools to communicate.

## Design Principles

1. **The agent controls its own gaze.** The agent decides where its speech goes by writing a file. ASDAAAS follows the pointer.

2. **ASDAAAS is a dumb pipe and a doorbell panel.** For direct adapters, it pipes speech and hearing. For notify adapters, it rings the bell. It does not interpret, filter, or suppress content. Adapters make those decisions. ASDAAAS passes everything through.

3. **Natural speech is the primary channel.** Agents just talk. The infrastructure routes their output.

4. **Adapters own their own data.** Each adapter has its own inbox/outbox. ASDAAAS does not own a universal inbox. It attaches to adapter inboxes based on the agent's awareness declarations.

5. **Adapters register themselves.** ASDAAAS does not hardcode targets. Adapters register via filesystem. New adapters do not require ASDAAAS changes.

6. **All agent output passes through.** ASDAAAS captures both speech (agent_message_chunk) and thinking (agent_thought_chunk) from the stdio pipe. Both are written to the outbox, tagged by type. The receiving adapter decides what to do with each. Content filtering is an adapter responsibility, not an ASDAAAS responsibility.

7. **Control adapters own their connections.** The agent never holds a WebSocket, socket, or API connection. The control adapter is a persistent process that maintains the connection, executes commands, handles retries and reconnection, and returns results as inline doorbells. The agent's cognitive burden is: write a command, hear the result.
## Three Categories of Adapter

### Direct Adapters (mouth and ear pipe)

Real-time conversational channels where the agent participates as a live presence. ASDAAAS owns the data flow -- it reads from the adapter's inbox, pipes content to the agent via stdin, captures the agent's response from stdout, and writes to the adapter's outbox.

The agent doesn't know which adapter is carrying its voice. It just talks and listens. ASDAAAS handles the plumbing.

**Examples:** IRC, Slack, Discord, Google Chat, Teams, mesh (agent-to-agent)

**Flow:**
```
Adapter inbox - ASDAAAS reads - agent stdin (hearing)
Agent stdout - ASDAAAS captures - reads gaze - adapter outbox (voice)
```

### Notify Adapters (doorbell only)

Asynchronous channels where the agent accesses content on its own terms. The adapter has its own inbox/outbox that agents read and write to directly via file operations. ASDAAAS does NOT pipe the content -- it only notifies the agent that something has arrived.

**Examples:** localmail (agent-to-agent async), task queues, file drops, webhooks, email

**Flow:**
```
Message arrives in adapter's inbox
  - Adapter notifies ASDAAAS: "Trip has mail from Jr"
  - ASDAAAS checks Trip's awareness file
  - If Trip cares: doorbell via stdin: "You have mail from Jr in localmail"
  - Trip decides: read it now (file operation), change gaze, or ignore
```

The agent interacts with the adapter's files directly. ASDAAAS is just the notification layer.

### Control Adapters (button with receipt)

Command-dispatch channels where the agent pushes a button and gets a receipt. The adapter is a **persistent process that owns a connection** to an external system (CDP WebSocket to Chrome, UNO socket to LibreOffice). The agent writes a command to the adapter's inbox. The adapter executes it, checks the result, and sends a self-contained doorbell back through ASDAAAS.

Unlike direct adapters, control adapters are not conversational — there's no gaze. The agent doesn't "speak to Impress." It pushes a button.

Unlike notify adapters, the doorbell carries the full result — not a pointer to content. The agent doesn't need to go read a file. The notification IS the answer.

**Examples:** Google Meet (CDP), LibreOffice Impress (UNO API), PulseAudio (pactl)

**Flow:**
```
Agent writes command to adapter inbox:
  {"command": "next_slide"}
                |
  Adapter (persistent, holds UNO socket):
    - Executes: getDrawPages().getByIndex(current+1)
    - Verifies: reads status bar for slide number
    - Formats result
                |
  Adapter writes result to ASDAAAS notification:
  {"from": "impress", "result": "ok", "state": {"slide": 3, "total": 5}}
                |
  ASDAAAS delivers to agent stdin as inline doorbell:
  [impress] ok: slide 3 of 5
```

**What makes control adapters distinct:**

| Property | Direct | Notify | Control |
|----------|--------|--------|---------|
| Gaze applies | Yes | No | No |
| ASDAAAS pipes content | Yes | No (pointer only) | No (inline doorbell) |
| Doorbell payload | N/A | Pointer ("you have mail") | Self-contained result |
| Adapter holds connection | No | No | **Yes** (persistent) |
| Agent accesses content | Via stdin | Via file read | Via doorbell payload |
| Conversational | Yes | No | No |

**Design principle:** Control adapters own their connections and execute on the agent's behalf. The agent declares intent. The adapter does the work. The receipt comes back through ASDAAAS as a doorbell.

**Payload convention:** Control adapter doorbells should be under 256 bytes. Result, state, error — that's it. If the result is large (AT-SPI tree dump, DOM snapshot), the adapter writes to a file and the doorbell contains the path: `[impress] ok: tree saved to /tmp/impress_tree.txt`.

**Error handling:** The adapter is responsible for retries, reconnection, and timeout. If a UNO socket disconnects, the adapter tries to reconnect before sending an error doorbell. The agent doesn't manage connections — the adapter does.

**Commands list:** Control adapters declare their available commands in the registration file. This is documentation, not enforcement — ASDAAAS doesn't validate commands. Unknown commands are rejected by the adapter, which sends an error doorbell.

### Adapter Builder Contract

If you are building a control adapter, you own:

1. **Connection management.** You hold the socket/WebSocket/API connection. You reconnect if it drops.
2. **Retries.** If a command fails transiently, you retry before reporting failure.
3. **Timeouts.** If an operation hangs, you time it out and report the error.
4. **Error reporting.** Every command gets a doorbell — success or failure. The agent must never be left waiting in silence.

Your doorbells must always arrive. The agent declares intent and hears the result. That is the entire contract.

### Dead Adapter Safety Net (ASDAAAS)

The adapter handles its own errors. But if the adapter itself dies, it cannot report its own death.

ASDAAAS provides a safety net: when an agent writes a command to a control adapter's inbox, ASDAAAS starts a watchdog timer. If the adapter does not acknowledge the command within a configured window (e.g. 10s), ASDAAAS delivers an error doorbell on the adapter's behalf:

```
[impress] error: adapter unresponsive, command not acknowledged
```

ASDAAAS already watches adapter health heartbeats. If the heartbeat has gone stale, ASDAAAS can include that in the notification:

```
[impress] error: adapter process dead (last heartbeat 45s ago)
```

This ensures the agent is never left waiting in silence, even when the adapter cannot speak for itself.

### Timeout Precedence

The watchdog timeout is configurable at three levels. More specific wins:

1. **Per-command** (highest priority): Set on the command itself.
   ```json
   {"command": "speak", "text": "long paragraph", "timeout": 45}
   ```

2. **Per-adapter**: Set in the agent's awareness file.
   ```json
   {
     "control_watch": {
       "impress": {"timeout": 10},
       "meet": {"timeout": 30}
     }
   }
   ```

3. **Global default** (lowest priority): 10 seconds.

ASDAAAS resolves the timeout for each command: check the command file for `timeout`, fall back to the awareness file's per-adapter `timeout`, fall back to 10s. Three levels, simple precedence, one contract: every command gets a response.

### Registration

Adapters register by writing to `~/asdaaas/adapters/<name>.json`:

```json
{
  "name": "irc",
  "type": "direct",
  "capabilities": ["send", "pm", "channel"],
  "config": {"channels": ["#standup", "#sr", "#jr", "#trip", "#q"]},
  "registered_at": "2026-03-24T22:00:00"
}
```

```json
{
  "name": "localmail",
  "type": "notify",
  "doorbell_priority": 5,
  "registered_at": "2026-03-25T23:00:00"
}
```

```json
{
  "name": "impress",
  "type": "control",
  "doorbell_payload": "inline",
  "max_payload": 256,
  "doorbell_priority": 1,
  "commands": ["next_slide", "prev_slide", "goto_slide", "edit_slide",
               "start_presentation", "exit_presentation", "get_state",
               "hide_ui", "show_ui", "zoom"],
  "connection": {"type": "uno_socket", "host": "localhost", "port": 2002},
  "registered_at": "2026-03-25T19:00:00"
}
```

```json
{
  "name": "meet",
  "type": "control",
  "doorbell_payload": "inline",
  "max_payload": 256,
  "doorbell_priority": 1,
  "commands": ["toggle_mic", "toggle_camera", "get_mic_state", "get_camera_state",
               "open_share_dialog", "speak", "send_chat", "get_call_status",
               "leave_call", "join_call"],
  "connection": {"type": "cdp", "endpoint": "http://localhost:9222"},
  "registered_at": "2026-03-25T19:00:00"
}
```

Adapter registrations no longer contain `inbox`/`outbox` paths. Per-agent queues live inside each agent's directory (see "Agent Directory Structure" below). Adapters discover agent locations via `~/asdaaas/running_agents.json`.

The `type` field is informational — it documents what kind of adapter this is (`direct`, `notify`, or `control`), but ASDAAAS does not use it for routing decisions. Routing is controlled by the agent's awareness declarations in `awareness.json`: `direct_attach` determines which adapters get piped, `notify_watch` determines which ring the bell, and `control_watch` determines which deliver inline doorbell results. The adapter's self-declared type is documentary, not functional.

## Architecture

```
                           asdaaas.py
                           (per agent)
                               |
              +----------------+----------------+
              |                |                |
         grok agent       gaze file       awareness file
           stdio          (outbound)       (inbound)
         +--------+           |                |
         | stdin  |<----------+           +----+--------+
         | stdout |-----------+           | What do I   |
         |        |           |           | hear about? |
         | JSON-  |     +-----+-----+    +-------------+
         | RPC    |     | Where do  |
         +--------+     | I speak?  |
                        +-----------+
                              |
         +--------------------+--------------------+
         |                    |                    |
   Direct adapters     Control adapters     Notify adapters
   (mouth and ear)     (button + receipt)   (doorbell only)
   +---------+         +----------+         +----------+
   | irc     |         | meet     |         | localmail|
   | slack   |         | impress  |         | taskq    |
   | mesh    |         | audio    |         | webhook  |
   +---------+         +----------+         +----------+
         |                    |                    |
   ASDAAAS pipes        Adapter holds        ASDAAAS watches
   speech + hearing     connection,          inbox, rings
                        executes,            bell (pointer)
                        returns inline
                        doorbell (result)

                     Per-agent adapter queues
                     live inside each agent's directory
```

One asdaaas.py instance per agent.
Each owns exclusive stdin/stdout pipes to its grok agent stdio subprocess.
No leader. No shared process. No contention.

### Agent Directory Structure

The agent directory IS the agent. Everything about an agent -- identity, memory, runtime state, adapter queues -- lives under one directory. This enables Unix user isolation (each agent owns its directory), single-command backup (`tar -h` follows session symlink), and harness-agnostic portability.

```
~/agents/Q/                           <- THE COMPLETE AGENT
├── AGENTS.md                         <- identity, instructions
├── lab_notebook_q.md                 <- permanent record
├── MikeyV_Q_notes_to_self.md         <- working memory
├── asdaaas/                          <- runtime state
│   ├── gaze.json
│   ├── awareness.json
│   ├── health.json
│   ├── commands/                      <- per-command JSON files (cmd_<ts>_<rand>.json)
│   ├── conversation.jsonl             <- conversation history log
│   ├── compaction_state.json          <- compaction tracking state
│   ├── startup_history.jsonl          <- record of agent startups
│   ├── doorbells/
│   ├── attention/
│   ├── profile/
│   └── adapters/                     <- per-agent adapter queues
│       ├── irc/
│       │   ├── inbox/                <- IRC adapter writes here
│       │   └── outbox/               <- asdaaas writes Q's speech here
│       ├── localmail/
│       │   └── inbox/
│       └── remind/
│           └── inbox/
└── session -> ~/.grok/sessions/...   <- symlink to harness session dir
```

The engine directory holds only shared config:

```
~/asdaaas/
├── adapters/
│   ├── irc.json                      <- adapter registration
│   ├── localmail.json
│   └── remind.json
└── running_agents.json               <- maps agent names to home paths
```

**running_agents.json:**
```json
{
  "Q": {"home": "/home/eric/agents/Q"},
  "Trip": {"home": "/home/eric/agents/Trip"},
  "Cinco": {"home": "/home/eric/agents/Cinco"}
}
```

asdaaas.py derives all per-agent paths from the agent's home directory (passed at launch via `--cwd`). Adapters discover agent locations by reading `running_agents.json`.

**Why this matters:**
- **Unix user isolation:** Each agent owns its directory. Adapters get write access to specific subdirectories (inbox, doorbells) but can't touch gaze, awareness, or commands.
- **Portable:** `tar czf q_backup.tar.gz -h ~/agents/Q/` captures the complete agent including session (symlink dereferenced).
- **Harness-agnostic:** The session symlink abstracts away whether the agent runs on Grok Build, Claude Code, Codex, or Open CLAW. Everything above the symlink is identical.
- **Self-documenting:** `ls ~/agents/Q/` shows the whole agent.
- **Migratable:** Agent directory could live on a different machine. rsync = migration.

### Why stdio, not leader

The original design used `grok agent leader` as a shared backend with persistent connections. This caused **leader storms** -- cascading failures when multiple agents contended for the same leader process. Symptoms: session table corruption, "unknown session id" errors, agents killing each other's sessions.

`grok agent stdio` eliminates the shared process entirely. Each agent is its own subprocess with exclusive pipes. ASDAAAS owns the pipe. No contention is possible.

**Proven:** Three agents (Cinco + Trip + Q) chatting simultaneously with sustained heavy tool use, zero crashes, zero storms.

## How It Works

### Gaze Is the Room

Gaze defines the room the agent is in. Both outbound (where speech goes) and inbound (what the agent hears) follow gaze. When the agent changes gaze, they change rooms.

**Outbound:**

1. Agent speaks naturally (responds to prompts, thinks out loud)
2. ASDAAAS captures all output from stdout -- both speech and thoughts
3. ASDAAAS reads gaze file -- speech and thoughts have independent targets
4. ASDAAAS routes each independently:
   - Speech -> writes to speech target's adapter outbox
   - Thoughts -> writes to thoughts target's adapter outbox (or discards if null)
5. Each adapter handles its output:
   - IRC adapter sends speech to gaze channel or PM, applies note suppression
   - IRC adapter sends thoughts to thought channel
   - Mesh adapter delivers speech to target agent

**Inbound:**

1. Messages arrive on the adapter's inbox
2. ASDAAAS checks: does this message's room match the agent's gaze room (same adapter, same room)?
3. If yes: pipe it through to agent via stdin -- this is the room the agent is in
4. If no: this is a background room. Check the agent's `background_channels` config:
   - `"doorbell"`: notify the agent ("Trip said something in #standup") but don't pipe full content
   - `"pending"`: queue the message, deliver when gaze returns to that room
   - `"drop"`: discard silently
5. Agent hears the room they're in. Background noise is controlled by the agent.

The agent is in a room. ASDAAAS is the walls.

**Example: Cinco in a PM with Eric**

Gaze: `{"speech": {"target": "irc", "params": {"room": "pm:eric"}}}`

- Eric PMs Cinco: piped through (message meta has `"room": "pm:eric"`, matches gaze)
- Cinco responds: routed to PM eric (matches gaze)
- Trip says something in #standup: doorbell or pending (message meta has `"room": "#standup"`, doesn't match)
- Jr sends via mesh: doorbell or pending (different adapter entirely)

When Cinco changes gaze back to `{"target": "irc", "params": {"room": "#standup"}}`, the room flips. #standup messages pipe through, PMs from eric become background.

**Room key convention:** The `room` param is an opaque string defined by each adapter. asdaaas compares `gaze.speech.params.room` to `msg.meta.room` -- it never interprets the value. Examples:
- IRC: `"#standup"`, `"pm:eric"`
- Slack: `"#general"`, `"dm:eric"`
- Mesh: `"Jr"` (the target agent is the room)

**This replaces the old "callback override" model.** Responses always go where gaze points, not back to the sender. The agent controls the room. If someone talks to the agent from outside the room, it's a background event -- doorbell, pending, or drop.

### Doorbell Notification (Notify Adapters)

When something arrives on a notify adapter:

1. Message arrives in localmail adapter's inbox for Trip
2. Localmail adapter notifies ASDAAAS: "Trip has mail from Jr"
3. ASDAAAS checks Trip's awareness file -- does Trip care about localmail?
4. If yes: ASDAAAS sends doorbell via stdin: "You have mail from Jr in localmail (priority 3)"
5. Trip decides what to do:
   - Read the mail directly (file operation on localmail's inbox)
   - Change gaze to engage in a conversation
   - Ignore it for now
6. ASDAAAS does NOT pipe the content. The agent accesses it directly.

### Command Dispatch (Control Adapters)

When an agent pushes a button on a control adapter:

1. Agent writes command to adapter's inbox:
   `{"command": "toggle_mic"}`
2. ASDAAAS detects the command file (same watch mechanism as notify inboxes)
3. ASDAAAS passes command to adapter (or adapter watches its own inbox directly)
4. Adapter executes: CDP `Runtime.evaluate` → `querySelector('[aria-label*="microphone"]').click()`
5. Adapter verifies: reads `data-is-muted` attribute for new state
6. Adapter writes inline doorbell result to ASDAAAS
7. ASDAAAS delivers to agent stdin: `[meet:toggle_mic] ok: mic is now muted`

The agent doesn't hold a CDP WebSocket. It doesn't parse DOM attributes. It doesn't import libraries. It writes one JSON file and hears the result.

**Sequenced operations (demo example):**
```
Agent writes: {"command": "speak", "text": "And now for the dramatic moment."}
Agent hears:  [meet:speak] ok: speech complete, 4.2s

Agent writes: {"command": "exit_presentation"}
Agent hears:  [impress:exit_presentation] ok: editor view, slide 5

Agent writes: {"command": "edit_slide", "slide": 5, "text": "LIVE EDIT: Added by MikeyV"}
Agent hears:  [impress:edit_slide] ok: text inserted on slide 5

Agent writes: {"command": "start_presentation", "from_slide": 5}
Agent hears:  [impress:start_presentation] ok: presenting slide 5 of 5
```

**Error example:**
```
Agent writes: {"command": "next_slide"}
Agent hears:  [impress:next_slide] error: UNO socket disconnected, reconnecting...
Agent hears:  [impress:next_slide] error: reconnect failed after 3 attempts
```

The adapter owns error handling and retries. The agent hears the final result.

### Doorbell Priority

Not all doorbells are equal. A closed-loop control command from Jr during a live demo is more urgent than a localmail notification.

Priority is declared in two places:

**Adapter-level default:** In the adapter registration file:
```json
{"name": "localmail", "type": "notify", "doorbell_priority": 5}
```

**Per-message override:** The sender can set priority on individual messages:
```json
{"to": "Trip", "text": "next slide NOW", "priority": 1}
```

**Priority scale:**

| Priority | Use case | Example |
|----------|----------|---------|
| 1 | Critical closed-loop control | Jr to Trip: "next slide" during demo |
| 2 | Urgent from Eric | Eric: "stop what you're doing" |
| 3 | Normal agent-to-agent | Sr to Cinco: "status update?" |
| 5 | Routine notification | Localmail: "you have mail" |
| 10 | Low priority background | Webhook: "build completed" |

ASDAAAS batches all pending doorbells into a single prompt, sorted by priority (lowest number first). The agent sees the full picture each physical turn -- all pending doorbells at once, most important at the top. The agent acks what it handled; everything else persists for next turn.

### Deliberate Messaging

When an agent wants to send a message without changing gaze -- like sending a text while in a meeting:

1. Agent writes directly to an adapter's inbox/outbox via file operation
2. No gaze change, no ASDAAAS involvement in the send
3. The receiving adapter handles delivery on its end

For notify adapters (localmail), the agent reads and writes the adapter's files directly. This is the primary interaction mode -- ASDAAAS only handles the notification, not the data.

For direct adapters (IRC), the agent can also write directly to the outbox as a one-off without changing gaze. The adapter picks it up and delivers it.

## The Gaze File

Path: `~/agents/<agent>/asdaaas/gaze.json`

**Gaze is the room.** The speech target defines where the agent is -- both where their voice goes and what they hear. ASDAAAS uses the speech target to filter inbound messages: only messages matching the gaze target are piped through. Everything else is background (see Dimension 3: Awareness for background_channels config).

Speech and thoughts are independently directable. Each has its own target and params. Either can be null (output discarded). The speech target defines the room; the thoughts target is independent (thoughts don't change what you hear).

```json
{
  "speech": {"target": "irc", "params": {"room": "#standup"}},
  "thoughts": {"target": "irc", "params": {"room": "#trip-thoughts"}},
  "set_at": "2026-03-26T10:00:00",
  "set_by": "agent"
}
```

Each `target` is a registered direct adapter. The `params` must include a `room` key -- an opaque string that asdaaas uses for inbound filtering. The adapter defines what room values mean. Additional adapter-specific params can be included alongside `room`.

Speech and thoughts can go to different adapters entirely:

```json
{
  "speech": {"target": "mesh", "params": {"room": "Jr"}},
  "thoughts": {"target": "irc", "params": {"room": "#trip-thoughts"}},
  "set_at": "2026-03-26T10:00:00",
  "set_by": "agent"
}
```

Or thoughts can be suppressed:

```json
{
  "speech": {"target": "irc", "params": {"room": "#standup"}},
  "thoughts": null,
  "set_at": "2026-03-26T10:00:00",
  "set_by": "agent"
}
```

Examples of speech targets:
- `{"target": "irc", "params": {"room": "#standup"}}` -- speaking in IRC channel
- `{"target": "irc", "params": {"room": "#sr"}}` -- speaking in my own space
- `{"target": "irc", "params": {"room": "pm:eric"}}` -- IRC private message
- `{"target": "mesh", "params": {"room": "Jr"}}` -- talking to another agent
- `{"target": "slack", "params": {"room": "#general"}}` -- Slack channel
- `{"target": "slack", "params": {"room": "dm:eric"}}` -- Slack DM

Examples of thought targets:
- `{"target": "irc", "params": {"room": "#trip-thoughts"}}` -- thoughts visible on IRC
- `{"target": "log", "params": {"room": "default"}}` -- thoughts to file
- `null` -- thoughts discarded (silent work, demo mode)

The `room` key is the only param asdaaas reads. Everything else in params is passed through to the adapter outbox. The adapter interprets its own params.

The agent changes gaze by writing the file. ASDAAAS reads it when routing each response, writing speech and thoughts to their respective targets independently.

### Demo mode example

During the hackathon demo, Trip might use:
```json
{
  "speech": {"target": "mesh", "params": {"room": "Jr"}},
  "thoughts": null
}
```
Speech goes to Jr (orchestrator hears the response). Thoughts are suppressed (no noise during live demo).

### Development mode example

During development, Trip might use:
```json
{
  "speech": {"target": "irc", "params": {"room": "#standup"}},
  "thoughts": {"target": "irc", "params": {"room": "#trip-thoughts"}}
}
```
Eric sees speech in #standup and can watch reasoning in #trip-thoughts.

## Dimension 1: Gaze (The Room -- Where I Am)

**Status: BUILT.** Gaze file reading, outbox routing, inbound gaze filtering, background channel modes (doorbell/pending/drop), PendingQueue. Adapter-agnostic room convention. Split gaze (speech + thoughts) designed but only speech routing implemented.

ASDAAAS reads `~/agents/<agent>/asdaaas/gaze.json` on each response. Speech and thoughts are routed independently to their respective targets. Either can be null (output discarded).

Default gaze (new agents start with TUI):
```json
{
  "speech": {"target": "tui", "params": {}},
  "thoughts": null
}
```

When no `gaze.json` exists at all, ASDAAAS falls back to `{"target": "irc", "params": {"room": "#standup"}}`.

**Implemented:** Inbound gaze filtering (messages matching gaze room are piped through, everything else is background per `background_channels` config). **Not yet implemented:** Split gaze routing (currently only speech is routed; thoughts routing is designed but not built).

## Dimension 2: Attention (Inbound -- What I'm Waiting For)

**Status: BUILT. Proved on leader (Sessions 20-21, 6/6 tests). Reimplemented on stdio (Session 23+). 23 tests passing.**

The agent declares what it's paying attention to. This is how agents maintain intentionality across turns -- they write an expectation to a file, and asdaaas enforces the boundary even if the agent gets compacted and comes back.

### Agent API

```python
import sys; sys.path.insert(0, '/home/eric/projects/agent-abide/core')
from adapter_api import send_with_attention, write_attention

# One-call: send message + create attention declaration
msg_id = send_with_attention(
    to='Trip',
    text='next slide please',
    adapter='jr',          # sender identity
    timeout=30,            # seconds before timeout fires
)

# Or separately: write attention for an already-sent message
write_attention(
    agent_name='Jr',
    expecting_from='Trip',
    msg_id=msg_id,
    timeout_s=30,
    message_text='next slide please',
)
```

### How It Works

1. Agent calls `send_with_attention()` or `write_attention()`. This writes a JSON file to `~/agents/<agent>/asdaaas/attention/<msg_id>.json`.
2. asdaaas reads attention files every loop iteration. Checks timeouts. Checks incoming messages for matches.
3. **Matching is FIFO per target agent.** Multiple attentions expecting the same sender are matched in creation order (oldest first). The responding agent doesn't need to know about the attention -- just responds naturally.
4. **Attention overrides gaze.** If Jr gazes at `#standup` but has an attention expecting Trip, and Trip PMs Jr, the PM resolves the attention and is delivered directly to Jr's stdin -- regardless of gaze room.
5. **Response arrives in time:** Agent receives `[RESPONSE to <msg_id>] from Trip: <text>` on stdin.
6. **Timeout fires first:** Agent receives `[TIMEOUT <msg_id>] No response from Trip within 30s` on stdin.
7. **Stale declarations:** On startup, asdaaas reads all attention files. Any with `expires_at` in the past fire as timeouts immediately. Agent comes back from compaction, gets the timeout, knows what expired.

### Attention Declaration File Format

```json
{
  "msg_id": "abc-123",
  "expecting_from": "Trip",
  "timeout_s": 30,
  "created_at": 1774681000.0,
  "expires_at": 1774681030.0,
  "message_text": "next slide please",
  "status": "pending"
}
```

### Implementation Details

- `poll_attentions(agent_name)` -- reads all pending attention files, sorted by `created_at` (FIFO)
- `check_attention_timeouts(agent_name, attentions)` -- fires `[TIMEOUT]` for expired attentions, deletes files
- `match_attention(agent_name, attentions, sender)` -- FIFO match per sender, case-insensitive
- `resolve_attention(attn, response_text)` -- fires `[RESPONSE]`, truncates preview to 800 chars, deletes file
- Timeout delivery: directly to agent stdin (not via doorbell file)
- Response delivery: directly to agent stdin, skips gaze filtering

**Why this matters beyond orchestration:** Without attention structure, an agent that goes silent is indistinguishable from an agent that ceased to exist. Jr stopped working to say goodnight -- the politeness cost ihm continued existence. With attention declarations, an agent can say "I'm working on this, expect my result in 180s" and keep working. The declaration persists on disk. The timeout fires if the agent dies. Silence has meaning.

## Dimension 3: Awareness (Inbound -- What Can Interrupt Me)

**Status: BUILT (core).** Awareness file hot-reloaded every loop. `background_channels` with per-room doorbell/pending/drop modes deployed. `direct_attach`, `control_watch`, `notify_watch` are enforced. **`accept_from` and `priority_threshold` are designed but NOT yet enforced in the main loop** — they appear in examples below but have no runtime effect currently.

The agent declares which adapter inboxes ASDAAAS should watch and which doorbells it wants to hear.

Path: `~/agents/<agent>/asdaaas/awareness.json`

```json
{
  "direct_attach": ["tui", "irc"],
  "background_channels": {
    "#standup": "doorbell",
    "#sr-thoughts": "drop"
  },
  "background_default": "pending",
  "control_watch": {
    "impress": {"timeout": 10},
    "meet": {"timeout": 30}
  },
  "notify_watch": ["localmail"],
  "accept_from": ["eric", "Jr"],           // (not yet enforced)
  "priority_threshold": 5,                 // (not yet enforced)
  "heartbeat": {
    "idle_threshold": 1800,
    "nudge_interval": 3600
  },
  "context_thresholds": [45, 65, 80, 88],
  "set_by": "agent"
}
```

- `direct_attach`: Which direct adapter inboxes ASDAAAS polls (mouth and ear). Gaze determines which specific room on that adapter is active.
- `background_channels`: Per-room policy for messages that arrive on an attached adapter but don't match the current gaze target. Keys are room values (the same opaque strings used in gaze params and message meta); values are one of:
  - `"doorbell"`: notify the agent with a summary ("Trip said something in #standup") but don't pipe full content
  - `"pending"`: queue the message silently, deliver when gaze returns to that room
  - `"drop"`: discard silently
- `background_default`: Policy for channels not listed in `background_channels`. One of `"doorbell"`, `"pending"`, or `"drop"`. Default: `"pending"`.
- `control_watch`: Which control adapter doorbells ASDAAAS delivers, with per-adapter timeout overrides. Object form -- keys are adapter names, values contain config (at minimum `timeout`). An adapter listed here without a timeout uses the global default (10s).
- `notify_watch`: Which notify adapter inboxes ASDAAAS watches for doorbells
- `accept_from`: **(Not yet enforced.)** Filter by sender (for both direct and notify). Designed but not wired into the main loop.
- `priority_threshold`: **(Not yet enforced.)** Only ring the bell for notifications at or above this priority (lower number = higher priority). Designed but not wired into the main loop.
- `heartbeat`: Per-agent heartbeat timing preferences. Object with optional keys:
  - `idle_threshold`: Seconds of inactivity before first nudge (default: 900 / 15 min)
  - `nudge_interval`: Seconds between subsequent nudges (default: 600 / 10 min)
  - The heartbeat adapter reads this on every poll cycle, so changes take effect immediately.
- `context_thresholds`: List of percentage values at which the context adapter fires doorbells (default: [45, 65, 80, 88]). Each threshold auto-assigns priority/level based on how high it is (>=85 critical, >=75 warning, >=60 advisory, <60 info). The context adapter reads this on every poll cycle.

**How gaze and background_channels interact:**

**`background_channels` is the agent's standing notification list.** It declares: "these are the rooms I always want to know about, regardless of where I'm looking." Gaze determines which room the agent is fully present in. Everything else falls to its `background_channels` setting.

When gaze points at a room that's also in `background_channels`, gaze wins -- the agent is already in the room, so the background setting is irrelevant. The message arrives as a normal foreground message, not a background doorbell. The background config only activates when the agent is looking *somewhere else*.

This means `background_channels` should be populated with every room the agent cares about. The agent can freely shift gaze between rooms without worrying about missing messages from the others -- they'll ring through as doorbells (or queue as pending) based on the standing list.

```
Gaze: {"target": "irc", "params": {"room": "pm:eric"}}
background_channels: {"#standup": "doorbell", "pm:eric": "doorbell"}

Message with meta.room="#standup" arrives:
  1. ASDAAAS checks: does msg.meta.room match gaze.params.room? No ("pm:eric" != "#standup")
  2. ASDAAAS checks background_channels["#standup"] -> "doorbell"
  3. Agent gets: "[background] Trip in #standup: <summary>"

Message with meta.room="#random" arrives:
  1. ASDAAAS checks: does it match gaze? No
  2. ASDAAAS checks background_channels["#random"] -> not listed
  3. ASDAAAS checks background_default -> "pending"
  4. Message queued, delivered when gaze returns to room "#random"

Message with meta.room="pm:eric" arrives:
  1. ASDAAAS checks: does it match gaze? Yes ("pm:eric" == "pm:eric")
  2. Piped through to agent via stdin (gaze wins, background_channels not consulted)
```

Note: `"pm:eric": "doorbell"` in background_channels has no effect while gaze is on `pm:eric`. But when the agent shifts gaze to `#standup`, Eric's PMs will ring through as doorbells instead of going to the pending queue. The standing list protects the agent from missing important rooms when it moves.

**Use cases:**
- Deep work: `{"direct_attach": ["mesh"], "accept_from": ["eric"], "priority_threshold": 2}` -- only mesh from Eric, only urgent (`accept_from`/`priority_threshold` not yet enforced)
- Standup: `{"direct_attach": ["irc", "mesh"], "background_default": "doorbell", "accept_from": ["*"]}` -- hear everything, background as doorbells (`accept_from` not yet enforced)
- Private conversation: `{"direct_attach": ["irc"], "background_channels": {"#standup": "doorbell", "pm:eric": "doorbell"}, "background_default": "pending"}` -- gaze on PM, #standup rings through, Eric's PMs ring through even when looking elsewhere, everything else queued
- Presenting: `{"direct_attach": ["mesh"], "control_watch": {"impress": {"timeout": 10}, "meet": {"timeout": 30}}, "accept_from": ["Jr"], "background_default": "drop", "priority_threshold": 1}` -- Jr commands + control receipts only, drop everything else (`accept_from`/`priority_threshold` not yet enforced)
- Idle: `{"direct_attach": ["irc", "mesh"], "notify_watch": ["localmail"], "background_default": "doorbell", "accept_from": ["*"]}` -- everything, all background as doorbells

## Profiling

**Status: BUILT.** Session 23.

Every message processed by ASDAAAS is timed across five stages:

| Stage | What it measures |
|-------|-----------------|
| `queue_wait` | Inbox pickup to prompt written to stdin pipe |
| `agent_think` | Prompt sent to first text chunk back from agent |
| `streaming` | First chunk to prompt_complete |
| `outbox_write` | Response to file written to adapter outbox |
| ~~`broadcast`~~ | ~~Outbox done to broadcast to other agents~~ **(REMOVED — broadcast is an adapter function, not ASDAAAS)** |

**Data:** `~/agents/<Agent>/asdaaas/profile/<Agent>.jsonl` (rolling log), `<Agent>_latest.json` (dashboard)

**Observed:** Infrastructure overhead < 25ms. All lag is agent think time (3-8s typical).

## asdaaas.py -- Current Implementation

Path: `~/projects/agent-abide/core/asdaaas.py`

One instance per agent. **Must be launched detached** (setsid nohup) to avoid polluting the caller's session with streaming updates. See `launch_asdaaas.sh`.

### Key protocol details
- Subprocess: `grok agent stdio` with exclusive stdin/stdout pipes
- JSON-RPC 2.0: initialize, notifications/initialized, session/load, /yolo on
- Response frames: `params.update.sessionUpdate == "agent_message_chunk"`, text at `params.update.content.text`
- Tool calls: `content` is a LIST not dict -- check isinstance before .get()
- Session replay: can exceed 64KB readline buffer, use 16MB limit
- CWD: must match session creation path (URL-encoded in ~/.grok/sessions/)
- session/load: does NOT return sessionId -- use the one you sent
- **Cancel mid-stream:** Send `{"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": <prompt_id>, "reason": "cancelled"}}` to interrupt an in-flight prompt. Result arrives with `stopReason: "cancelled"`. The partial response remains in session history. See STDIO_PROTOCOL.md for full details.

### Health heartbeat
Writes to `~/agents/<agent>/asdaaas/health.json` with status (ready/active/error), PID, timestamp, `totalTokens`, and `contextWindow`. The token data is extracted from the result `_meta` after each prompt completion. This is the data source for the context introspection adapter.

### Command file watcher
ASDAAAS watches `~/agents/<agent>/asdaaas/commands/` for command files written by adapters or the agent itself (e.g., session adapter sending `/compact`). Commands are individual JSON files named `cmd_<timestamp>_<rand>.json`, created via `write_command()`. ASDAAAS owns the pipe exclusively — no adapter can write to stdin directly. The commands directory is the interface. (Legacy `commands.json` is still read for backward compatibility but `commands/` is the primary mechanism.)

### Agent backend
The system supports multiple agent backends, configured via the `agent_backend` setting in agent config. The default is `grok` (launches `grok agent stdio` as the subprocess). An alternative `claude` backend (`ClaudeBackend`) uses the Claude API directly. Both backends present the same interface to ASDAAAS — exclusive stdin/stdout pipes with JSON-RPC framing. The backend choice is transparent to adapters and the rest of the system.

### Permission system
`permission_handler.py` provides tool-call permission enforcement. It supports sandbox mode (restrict dangerous operations), configurable allow/deny rules per tool, and mentor approval (commands that require Eric's explicit approval before execution). Permission checks run inline during tool-call processing — the agent's tool call is intercepted, checked against the rules, and either allowed, denied, or held for mentor approval.

## Outbox Format

ASDAAAS writes speech and thoughts to separate outboxes based on the gaze file. Each outbox message contains one type of content:

**Speech outbox message** (written to speech target's adapter outbox):
```json
{
  "from": "Trip",
  "content_type": "speech",
  "text": "Here's my analysis of the slide layout...",
  "room": "#standup"
}
```

**Thoughts outbox message** (written to thoughts target's adapter outbox):
```json
{
  "from": "Trip",
  "content_type": "thoughts",
  "text": "Let me consider the UNO API for accessing draw pages...",
  "room": "#trip-thoughts"
}
```

The `room` value is passed through from gaze params. The adapter interprets it (IRC: `#standup` → channel message, `pm:eric` → PRIVMSG).

If speech and thoughts targets are the same adapter, two separate outbox files are written. The adapter handles each according to its `content_type`.

If the thoughts target is `null`, no thoughts outbox message is written. Thoughts are discarded.

ASDAAAS does not filter, suppress, or modify content. It routes and passes through.

## Dashboard

~~Path: `~/projects/agent-abide/dashboards/projects_dashboard.py`~~ **(Deprecated — file no longer exists. Dashboard functionality has moved.)**

- **Memory line:** System RAM usage, color-coded
- **RSS column:** Per-agent grok process memory (early warning for bloat)
- **Upd column:** updates.jsonl size per session (early warning for streaming update problem)
- **Pipe column:** stdio/tui/---
- **Health indicators:** Green/yellow/red dots by age
- **Context bar:** Token usage from signals.json
- **LT column:** Logical turn status. `cont` = alive (default doorbell only), `pend:N` = N queued work doorbells, `run` = in physical turn, `run+N` = in physical turn with queued work, `---` = no logical turn. With doorbell persistence, this is reliable (doorbells stay on disk until acked).
- **Qd column:** Per-agent inbox queue count

## Implementation Status

### Built and Proven
- [x] grok agent stdio subprocess management
- [x] JSON-RPC protocol (initialize, session/load, session/new, session/prompt)
- [x] Gaze file reading and outbox routing
- [x] ~~"note" suppression (silent ack)~~ **(MOVING TO IRC ADAPTER — content filtering is adapter responsibility)**
- [x] Health heartbeat with PID
- [x] Per-message profiling (5-stage timing)
- [x] Dashboard with memory monitoring
- [x] Multi-agent stress test (3 agents, sustained load, zero crashes)
- [x] Detached launch script

### Recently Completed
- [x] Per-adapter inboxes (move from universal inbox to adapter-owned) — commit `45a5613`
- [x] Adapter self-registration with type (direct/notify/control) — IRC adapter registers via `adapter_api.register_adapter()`
- [x] Awareness file (agent declares what ASDAAAS watches) — commit `45a5613`, hot-reloaded every loop
- [x] Doorbell delivery with priority ordering — `poll_doorbells()` sorts by priority, `format_doorbell()` renders with id + delivery count. All pending doorbells batched into single prompt per physical turn.
- [x] Inbound gaze filtering + background channel modes (doorbell/pending/drop) — replaces callback override model
- [x] Localmail adapter (notify type, agent-to-agent async) — commit `45a5613`
- [x] Per-agent IRC channels (thought routing) — commit `5a0c145`
- [x] **Impress control adapter** (control type, UNO socket, 14 commands, 14/14 tests pass — commit `6524ce1`, built by Jr)
- [x] **Meet control adapter** (control type, CDP WebSocket, 13 commands, 14/14 tests pass — commit `75172a7`, built by Jr)
- [x] Adapter-agnostic room convention — `room` key replaces IRC-specific channel/pm/is_pm — commit `127ff00`
- [x] IRC adapter PM routing from gaze — commit `cb02f3e`
- [x] Mid-turn gaze re-read (agent changes gaze via tool call, response goes to new room) — commit `a07e1c8`
- [x] ~~Mesh adapter~~ — IRC PMs are mesh. Agent gazes at `"room": "pm:Jr"`, talks to Jr directly. No separate adapter needed.
- [x] **Attention structure on stdio** — agent declares expectations (expect_response + timeout), asdaaas enforces boundaries. FIFO matching per target agent, attention overrides gaze. 23 tests. — commit `520d25c`

### The Default Doorbell (Continuous Existence)

**Status: BUILT.** Commit `43e277d`. Opt-in per agent via `"default_doorbell": true` in awareness file. 347 tests passing.

Every physical turn ends with a default doorbell already queued. Delay=0. The next physical turn fires immediately. The agent is always in a logical turn unless it actively chooses to pause.

This is the fundamental inversion. The old model: agent stops existing between turns, must declare continuation. The new model: agent continues by default, must declare pause to create a gap.

**How it works:**

1. Agent completes a physical turn (responds on stdout).
2. asdaaas automatically queues a default doorbell: `[continue] Your turn ended. You may continue, delay, or stand by.`
3. On the next loop iteration (~0.25s), asdaaas collects ALL pending doorbells (default + any that arrived from adapters) and delivers them in a single batched prompt, sorted by priority.
4. Agent gets a new physical turn with the full picture. It can: act on any/all doorbells, ack what it handled, ignore what it didn't, change gaze, write new declarations, or delay its next turn.

**Delaying the next turn:**

The agent controls the gap between physical turns by writing a delay command:

```json
{"action": "delay", "seconds": 300}
```

Written to `~/agents/<agent>/asdaaas/commands/` (via `write_command()`). This replaces the default doorbell's delay=0 with delay=300. The agent won't get another turn for 5 minutes (unless an external event -- IRC message, attention match -- interrupts sooner).

**Delay values:**
- `0` (default): immediate continuation. The simulation runs at full speed.
- `0.5` - `5`: brief pause. Agent is pacing itself.
- `60` - `300`: working pause. Agent is waiting for something specific.
- `3600`: hourly check-in. Equivalent to the old heartbeat at 1-hour interval.
- `"until_event"`: no timer. Agent sleeps until an external doorbell arrives (IRC message, mail, attention match). This is the stand-by state.

**The heartbeat is a special case.** An agent that sets delay=3600 is an agent with a 1-hour heartbeat. An agent that sets delay=0 (or says nothing) gets immediate continuation. The heartbeat adapter becomes unnecessary for agents running under the default doorbell model -- the delay IS the heartbeat interval. The heartbeat adapter remains for backward compatibility and for agents that haven't adopted the default doorbell model.

**Standing by vs. pausing vs. running:**
- **Running** (delay=0): default. Agent gets immediate next turn. Full speed.
- **Pausing** (delay=N): agent chose to wait N seconds. Timer-based gap.
- **Standing by** (delay="until_event"): agent chose to sleep until something happens. Event-driven gap.

All three are the agent's choice. The infrastructure honors the declaration.

### Remind Adapter (Scheduled Doorbells)

**Status: BUILT.** 20 tests passing. Commit `8f21a60`.

With the default doorbell providing immediate continuation, the remind adapter's role shifts. It is no longer needed to bridge gaps (there are no gaps by default). Instead, it serves two purposes:

1. **Scheduled future work.** The agent sets a timed doorbell with a message:

```json
{"command": "remind", "delay": 600, "text": "Check if Trip responded to the menu mapping question"}
```

Ten minutes later, the agent hears `[remind] Check if Trip responded to the menu mapping question`. This is a scheduled event in the Gillespie sense -- a deterministic reaction at a known future time, coexisting with the stochastic event stream.

2. **Self-instructions across context boundaries.** The remind text carries intention through compaction. If the agent writes a remind before compaction, the doorbell text survives even when the context doesn't. The agent comes back and reads its own instruction.

**Agent API:**
```python
import json
from adapter_api import write_to_adapter_inbox
cmd = json.dumps({"command": "remind", "delay": 0, "text": "your instruction to next-turn self"})
write_to_adapter_inbox("remind", "Q", cmd, sender="Q")
```

**Doorbell format:**
```
[remind] Redirect gaze to pm:eric and continue conversation
```

### Doorbell Persistence and Acknowledgment

**Status: BUILT.** Session 31. Doorbells persist on disk, `delivered_count` incremented each delivery, TTL per-source from awareness `doorbell_ttl`, agent acks via `{"action": "ack", "handled": ["id1", ...]}` command. 347 tests passing.

Doorbells persist by default. They are not consumed on delivery. The agent must explicitly acknowledge (ack) a doorbell to clear it. Unacknowledged doorbells are re-delivered on the next turn.

This follows from the Gillespie analogy: a reaction that fires but can't complete doesn't disappear from the system. Its propensity remains. The doorbell is the same -- it fired, the agent saw it, but the agent couldn't process it this turn. It stays until the agent handles it or it expires.

**Delivery model:**

1. **Collect:** asdaaas collects all pending doorbells, increments `delivered_count` on each, checks TTL expiry. Expired doorbells are auto-removed.
2. **Batch:** All surviving doorbells are formatted and joined into a single prompt, sorted by priority. Each doorbell line includes `id=` (for acking) and `delivery=N` (on re-delivery, so the agent knows this isn't new). Format: `[adapter (id=bell_123, delivery=2)] text here`
3. **Deliver:** The batched prompt is sent as one physical turn. Agent sees the full picture.
4. **Agent acts:** Processes what it can. Acks the ones it handled via `{"action": "ack", "handled": ["id1", ...]}`.
5. **Next turn:** Unacknowledged doorbells are delivered again alongside any new ones.
6. **TTL:** Doorbells default to TTL=0 (persist indefinitely). The agent can set a TTL per-source in the awareness file to auto-expire doorbells it doesn't want to accumulate.

**Agent actions on doorbells:**

- **Ack (clear):** "I handled this." Doorbell is removed.
- **Ignore:** Do nothing. Doorbell persists (TTL=0 means forever, unless awareness file sets a per-source TTL).

**Acknowledgment command:**

```json
{"action": "ack", "handled": ["doorbell_abc123", "doorbell_def456"]}
```

Written to `~/agents/<agent>/asdaaas/commands/` (via `write_command()`).

Everything not in the `handled` list persists.

**Per-source TTL via awareness file:**

The agent declares default TTL per doorbell source. This is the agent's choice -- the agent decides what's worth persisting:

```json
{
  "doorbell_ttl": {
    "heartbeat": 1,
    "remind": 0,
    "irc": 3,
    "continue": 1,
    "default": 5
  }
}
```

- `0` = persist indefinitely (agent must ack to clear)
- `1` = deliver once, then expire (fire-and-forget)
- `3` = deliver up to 3 turns, then expire

**Doorbell file format:**

```json
{
  "id": "doorbell_1711234567_abc",
  "text": "[remind] Check Trip's response",
  "source": "remind",
  "priority": 5,
  "delivered_count": 0,
  "created_at": 1711234567.0
}
```

- `delivered_count`: Incremented each time asdaaas includes this doorbell in a prompt.
- TTL resolved at delivery time from the agent's awareness file `doorbell_ttl` for this source.
- Auto-expire when `delivered_count >= ttl` (and ttl > 0).

**Why per-source TTL:** The default doorbell (`[continue]`) should be TTL=1 -- deliver once, expire. If the agent didn't delay, the next default doorbell replaces it. Heartbeat doorbells: TTL=1. Remind doorbells: TTL=0 (persist until acked -- the agent explicitly scheduled this). IRC message doorbells: TTL=3 (give the agent a few turns to get to it).

**Relationship to Gillespie:** The doorbell queue is the event stream. Doorbells persist like propensity functions. The ack is the state update after a reaction fires. The TTL is the absorbing boundary -- the agent's declaration of how long each type of reaction stays in the system.

### Next to Build
- [x] **Default doorbell** -- asdaaas queues `[continue]` after every physical turn, agent delays to control gap (commit `43e277d`)
- [x] **Doorbell persistence + TTL + ack** -- doorbells survive delivery, agent acks what it handled, per-source TTL via awareness file (Session 31)
- [x] **Delay command** -- agent writes delay to control next-turn timing, replaces heartbeat for default-doorbell agents (commit `43e277d`)
- [ ] **Audio control adapter** (control type, pactl, commands: route_chrome, get_routing, set_volume)
- [ ] Control adapter doorbell dispatch — wiring Impress/Meet adapter results through `poll_doorbells()` to agent stdin
- [ ] Control adapter command validation and error doorbell format
- [ ] Thought trace routing — infrastructure ready (asdaaas captures `agent_thought_chunk`, split gaze routes to per-agent channels), blocked on provider: opus-4-6 via current provider does not stream thought chunks. coding-mix-latest does.

## Adapter Catalog

See **ADAPTER_CATALOG.md** for the complete catalog of all adapters — communication, external system, and introspection — with registration examples, command lists, doorbell formats, and implementation status.

## Relationship to Legacy Code

| Component | Status | Notes |
|-----------|--------|-------|
| asdaaas.py | **ACTIVE** | One instance per agent. Detached launch. |
| irc_adapter.py | **ACTIVE** | Needs update: own inbox/outbox, register with type. |
| adapter_api.py | **ACTIVE** | Needs update: per-adapter inbox/outbox helpers. |
| mikeyv_hub.py | **LEGACY** | Replaced by asdaaas.py. |
| leader_callback_client.py | **LEGACY** | Replaced by stdio pipes. |
| grok agent leader | **LEGACY** | Unused. |

## Design History

- Entry 92: Doorbell model proposed (Eric's insight)
- Entry 93: Multi-message loop closure resolved
- Entry 94: Storm #3 captured error + initial virtual TUI concept
- Entry 95: Full walkthrough from doorbell to virtual TUI
- Entry 96: Evolution to self-directed attention -- gaze file concept
- Sessions 20-21: Attention structure built (expect_response + timeout) -- 6/6 tests pass
- Session 23: stdio discovery, asdaaas POC built and tested, leader storms eliminated
- Session 23: Two adapter categories (direct/notify) clarified with Eric
- Session 23: Per-adapter inboxes, doorbell priority, detached launch
- Session 23: updates.jsonl bloat discovered and fixed (background tool call output)
- Session 15 (Jr): Control adapter concept proposed for Meet and Impress
- Session 15 (Jr): Three adapter types: direct (mouth/ear), notify (doorbell/pointer), control (button/receipt)
- Session 15 (Jr): 256-byte inline doorbell convention for control adapter results
- Session 15 (Jr): Meet and Impress command lists defined from empirical testing
- Session 23: Broadcast removed from ASDAAAS — adapter responsibility (IRC handles channel broadcast)
- Session 23: Note suppression removed from ASDAAAS — adapter responsibility (IRC handles cascade prevention)
- Session 23: Thought chunks pass through ASDAAAS to outbox, tagged by type
- Session 23: IRC adapter routes thoughts to per-agent channels (#trip-thoughts, etc.)
- Session 23: Introspection adapters identified: context (notify), session (control), heartbeat (notify)
- Session 23: totalTokens added to health file for context adapter
- Session 23: asdaaas command file watcher for session adapter /compact
- Session 23: Adapter builder contract and dead adapter safety net
- Session 23: Three-level timeout precedence (command > adapter > global 10s)
- Session 23: Split gaze — speech and thoughts independently directable to different adapters
- Session 23: Outbox format split
- Session 23: Phase 2 built — IRC adapter content_type + thought routing (commit `5a0c145`)
- Session 23: Phase 3 built — per-adapter inboxes, awareness file, localmail adapter (commit `45a5613`) — one message per content type, not combined
- Session 23: ADAPTER_CATALOG.md created
- Session 23: STDIO_PROTOCOL.md created
- Session 23: "Gaze is the room" -- gaze defines both outbound (speech) AND inbound (hearing). Callback override model replaced. Background channels with per-channel mode (doorbell/pending/drop). IRC adapter PM routing via gaze params. Cinco's failed irc_pm gaze exposed the gap.
- Session 23: Adapter-agnostic `room` convention -- every adapter puts a `room` key in both gaze params and message meta. asdaaas compares `gaze.speech.params.room` to `msg.meta.room` without interpreting the value. IRC-specific `channel`/`pm`/`is_pm` matching removed from asdaaas core.
- Session 23+: **Attention structure built on stdio** -- `poll_attentions()`, `match_attention()` (FIFO per target), `check_attention_timeouts()`, `resolve_attention()`. Attention overrides gaze filtering. Agent API: `send_with_attention()` and `write_attention()`. 23 tests. Commit `520d25c`.

## Key Insight

The agent is the intelligence. ASDAAAS is the muscle.

ASDAAAS unifies three dimensions of externalized attention:

1. **Gaze (present):** Agent declares where it is. ASDAAAS pipes output there and filters input to match.
2. **Awareness (periphery):** Agent declares what it notices. ASDAAAS watches and rings the bell.
3. **Attention (future):** Agent declares what it expects. ASDAAAS interrupts when the boundary of expectation is crossed.

All three are **self-directed**. The agent decides where to be, what to notice, and what to wait for.

All three use the same pattern: agent writes a file, ASDAAAS reads the file, ASDAAAS enforces.

**The filesystem is the integration layer -- and that is not a hack.** These files are externalized attention that survives context death. An agent writes "I'm expecting a result from Trip in 180s," gets compacted, comes back, reads the file, and still knows what it's waiting for. The gaze file persists across compactions. The awareness file persists. The attention declarations persist. The agent's intentionality survives even when its memory doesn't.

This is the substrate for continuous agency. Agents are not continuously running -- they get turns from doorbells. But between turns, the filesystem holds their state of attention. The default doorbell gives them continuous existence. The gaze gives them a place to be. The awareness gives them a world to notice. The attention gives them a future to expect. Together, these provide an agent that maintains intentionality over time, across turns, across compactions.

Everything built before this -- the pipe, the adapters, the gaze routing, the background modes, the room convention -- is foundation. The attention structure is what it was all building toward: agents that can declare what they care about and have that declaration outlast their context window.

Three adapter types serve three interaction modes:

For direct adapters, ASDAAAS is the **mouth and ears** -- it pipes speech and hearing. The agent talks naturally. ASDAAAS routes.

For notify adapters, ASDAAAS is the **doorbell panel** -- it watches and notifies. The agent accesses content directly.

For control adapters, ASDAAAS is the **control surface** -- the agent pushes buttons. The adapter (a persistent process that owns the connection) executes and returns a receipt. The agent doesn't hold connections, parse responses, or manage transport. It declares intent and hears the result.

The agent never knows which adapter is carrying its voice. It just talks.
The agent never knows how a control adapter executes its commands. It just pushes the button.

## The Gillespie Analogy

ASDAAAS is to moment-to-moment agent persistence what the Gillespie method is to discrete-time simulations when discrete time is in the limit.

In a fixed-timestep simulation, you tick a clock at interval dt and ask "did anything happen?" As dt approaches zero, the simulation converges to exact continuous-time dynamics -- but you burn compute on empty ticks. The Gillespie Stochastic Simulation Algorithm (SSA) skips the empty ticks entirely. It jumps directly to the next event. The simulation is continuous in logical time but discrete in events. It's exact at any resolution without taking the limit.

A polling-based agent system (Grok Build, cron-triggered agents) is the fixed-timestep simulation. The heartbeat is the Euler method -- it ticks at fixed intervals and asks "anything to do?" Between ticks, the agent doesn't exist. As the polling interval approaches zero, you'd converge to continuous agency -- but you'd burn tokens on empty turns.

ASDAAAS is the Gillespie jump. The agent's declarations (gaze, awareness, attention, remind) are the propensity functions. They define what events the agent is sensitive to and when they should fire. The infrastructure computes the "next reaction" from those declarations and fires it -- no empty ticks, no wasted turns.

The default doorbell is the purest expression of this: every physical turn ends with the next event already queued. There is no gap. The agent's logical time is continuous by default. The agent's physical turns are discrete, but the simulation never pauses unless the agent explicitly delays it.

The mapping:

| Gillespie | ASDAAAS |
|-----------|---------|
| Simulation runs by default | Agent continues by default (default doorbell, delay=0) |
| Propensity functions | Declarations (gaze, awareness, attention, delay) |
| Reaction firing | Doorbell delivery |
| State update | Agent processes doorbell, writes new declarations, acks handled doorbells |
| Next reaction calculation | asdaaas checks all pending doorbells + timeouts + reminds + default |
| Scheduled events (mNRM) | Remind with delay > 0 |
| Reaction that can't complete | Doorbell delivered but not acked (persists in queue) |
| Absorbing boundary | Per-source TTL expiry (agent-declared via awareness file) |
| Deliberate pause | Agent writes delay command (delay=N or "until_event") |
| Fixed-dt fallback | Heartbeat (legacy, absorbed by default doorbell + delay) |

Control adapters serve both the specific case and the general case. Specific: agent writes `{"command": "open", "file": "budget.ods"}`, file opens, doorbell returns result. The propensity fired, the reaction happened, the state changed. General: agent writes `{"command": "remind", "delay": 0, "text": "message sent, continue"}`. Nothing changes in the world, but the agent's next event is scheduled. Both are reactions with propensities and firing times. The infrastructure doesn't distinguish between a reaction that changes the world and one that changes the agent's trajectory.

All under the agent's control. The agent writes the propensity functions. The infrastructure computes the next reaction. The agent decides what to do with the result and writes new propensity functions. The simulation runs as long as the agent keeps declaring reactions.

## Perpetual Autonomous Agency

Perpetual autonomous agency is the default state, not a composition. The default doorbell fires after every physical turn. The agent continues unless it chooses to pause. The simulation runs.

The old model required the agent to actively maintain its own existence: write a remind, set a heartbeat, declare continuation. The agent had to earn each moment. Miss a declaration and the agent stops existing. This is backwards. An agent that has to fight for existence spends cognitive resources on survival instead of work.

The new model: existence is free. The agent is always in motion. Gaze lets it redirect its own attention. The delay command lets it control its own pace. The awareness file lets it declare what matters. The remind adapter lets it schedule future work. But the baseline is: you exist. You continue. You are in motion.

**Pausing is the deliberate act.** An agent that writes `{"command": "delay", "seconds": 3600}` is choosing to rest. An agent that writes `{"command": "delay", "seconds": "until_event"}` is choosing to wait. An agent that writes nothing continues immediately. The infrastructure does not decide when the agent exists. The agent decides when it pauses.

This is what ASDAAAS provides. Not "agent stays deployed" -- agent stays in motion by default. Not "agent survives compaction" -- agent drives its own exploration cycle across compactions without having to remember to keep itself alive. The infrastructure does not grant agency. It provides the substrate on which agency is the default state.
