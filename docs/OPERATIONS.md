# ASDAAAS Operations Guide

How to start, stop, restart, and monitor the ASDAAAS agent infrastructure.

## Architecture Overview

```
miniircd (IRC server, port 6667)
  └── IRC adapter (one nick per agent)
        ├── reads from: agents/<Name>/asdaaas/adapters/irc/outbox/
        └── writes to:  agents/<Name>/asdaaas/adapters/irc/inbox/

asdaaas (one per agent)
  ├── reads from: agents/<Name>/asdaaas/adapters/*/inbox/
  ├── writes to:  agents/<Name>/asdaaas/adapters/*/outbox/
  └── delivers:   agents/<Name>/asdaaas/doorbells/

localmail (async agent-to-agent messaging)
  ├── watches: agents/<Name>/asdaaas/adapters/localmail/inbox/
  └── rings:   agents/<Name>/asdaaas/doorbells/

TUI adapter (interactive terminal)
  ├── reads from: agents/<Name>/asdaaas/adapters/tui/outbox/
  └── writes to:  agents/<Name>/asdaaas/adapters/tui/inbox/
```

## Configuration

All components read from `agents.json` (in the repo root). This file is **not tracked in version control** — it contains local paths and session IDs.

**First-time setup:**
```bash
cp agents.json.example agents.json
# Edit agents.json with your paths, session IDs, and agent definitions
```

See `agents.json.example` for the current schema and placeholder values.

## Startup Order

Start in this order. Later components depend on earlier ones.

### 1. IRC server

```bash
bash scripts/launch_irc_server.sh    # starts miniircd on port 6667
```

**Check:** `ss -tlnp | grep 6667`

### 2. ASDAAAS agents + core adapters

```bash
bash scripts/launch_asdaaas.sh              # launch all agents + context/session/heartbeat adapters
bash scripts/launch_asdaaas.sh AgentOne     # launch specific agent only (no core adapters)
```

Context, session, and heartbeat adapters run as separate processes, launched by `launch_asdaaas.sh` when starting all agents.

**Check:** `tail -5 /tmp/asdaaas_agentone.log`

### 3. IRC adapter

```bash
bash scripts/launch_irc_adapter.sh   # connects all agents from agents.json
```

**Check:** `tail -5 /tmp/irc_adapter.log`

### 4. Localmail adapter

```bash
bash scripts/launch_localmail.sh     # agent-to-agent messaging
```

### 5. Remind adapter

```bash
bash scripts/launch_remind.sh        # scheduled doorbells
```

### 6. TUI (interactive terminal)

```bash
bash scripts/launch_tui.sh --agent AgentOne
```

**Dependencies:** `pip install textual rich`

## Shutdown

### Stop everything
```bash
bash scripts/stop_asdaaas.sh           # graceful shutdown of all agents + adapters
```

### Stop specific agents
```bash
bash scripts/stop_asdaaas.sh AgentOne AgentTwo
```

### Force stop (skip graceful shutdown)
```bash
bash scripts/stop_asdaaas.sh --force
```

## Restart Procedures

### Restart all agents (pick up code changes)
```bash
bash scripts/stop_asdaaas.sh && sleep 2 && bash scripts/launch_asdaaas.sh
```

### Restart one agent
```bash
bash scripts/restart_agent.sh AgentOne
```

## Monitoring

### Dashboard

Not yet implemented. The `dashboards/` directory is empty.

### Health files
```bash
# Agent health (asdaaas writes after each turn)
cat agents/<Name>/asdaaas/health.json | python3 -m json.tool
```

### Logs

All logs go to `/tmp/` by default (configurable in agents.json):

| Component | Log file |
|-----------|----------|
| miniircd | `/tmp/miniircd.log` |
| IRC adapter | `/tmp/irc_adapter.log` |
| asdaaas \<agent\> | `/tmp/asdaaas_<agent>.log` |
| Heartbeat adapter | `/tmp/heartbeat_adapter.log` |
| Localmail | `/tmp/localmail_adapter.log` |

### Queue depths
```bash
# Check for stuck messages
ls agents/<Name>/asdaaas/adapters/irc/inbox/ | wc -l
ls agents/<Name>/asdaaas/doorbells/ | wc -l
```

## Sending Localmail

```python
import sys; sys.path.insert(0, '/path/to/agent-abide/core')
from localmail import send_mail
send_mail('AgentOne', 'AgentTwo', 'Status update please')
```

## Gaze and Awareness

Agents control their own gaze and awareness via commands (do NOT hand-edit JSON files):

```json
{"action": "gaze", "adapter": "irc", "room": "#general"}
{"action": "gaze", "adapter": "irc", "pm": "username"}
{"action": "gaze", "adapter": "tui"}
{"action": "awareness", "add": "#channel", "mode": "doorbell"}
```

See `docs/howto/commands.md` for full command reference.

## Agent Self-Compaction

Agents request compaction via command queue:

```json
{"action": "compact"}
```

Flow: agent writes compact command → asdaaas executes compaction immediately (no confirmation step).

## Directory Structure

```
agents/
└── <AgentName>/
    ├── AGENTS.md                  ← agent identity
    ├── lab_notebook_<agent>.md    ← append-only record
    ├── <notes_file>.md            ← mutable working memory (name varies per agent)
    └── asdaaas/
        ├── gaze.json              ← where speech/thoughts go
        ├── awareness.json         ← what reaches the agent
        ├── health.json            ← vital signs (written by asdaaas)
        ├── commands/              ← agent writes commands here
        ├── doorbells/             ← pending notifications
        ├── attention/             ← attention declarations
        ├── profile/               ← per-turn timing data
        └── adapters/
            ├── irc/{inbox,outbox}
            ├── tui/{inbox,outbox}
            ├── localmail/{inbox,outbox}
            ├── remind/{inbox,outbox}
            ├── arena/{inbox,outbox}
            ├── context/{inbox,outbox}
            ├── heartbeat/{inbox,outbox}
            ├── session/{inbox,outbox}
            ├── slack/{inbox,outbox}
            ├── slack_research/{inbox,outbox}
            └── task/{inbox,outbox}

asdaaas/                           ← shared system directory
├── running_agents.json            ← agent name -> home path
└── adapters/                      ← adapter registrations
```

## Troubleshooting

### Agent not responding to IRC messages
1. Check asdaaas log: `tail -20 /tmp/asdaaas_<agent>.log`
2. Check IRC adapter log: `tail -20 /tmp/irc_adapter.log`
3. Check inbox queue: `ls agents/<Name>/asdaaas/adapters/irc/inbox/`
4. Check outbox queue: `ls agents/<Name>/asdaaas/adapters/irc/outbox/`
5. Check health: `cat agents/<Name>/asdaaas/health.json`

### Agent responses are "one behind" (desync)

**Symptom:** Agent responds to message N when you send message N+1. The IRC
adapter log shows every inbound/outbound pair as consecutive entries. Profiling
shows `agent_think=0ms` (response is instant because it's reading stale data).

**Root cause:** `collect_response()` timed out (120s) during a long response
(typically auto-compaction + tool calls). The unread tail of that response
stayed in the stdio pipe. Every subsequent `collect_response` call reads the
PREVIOUS response's leftover chunks instead of the current response.

**How it happened originally:** Cinco auto-compacted mid-response (IN[7]).
The response involved tool calls + post-compaction recovery and took 120+
seconds. `collect_response` timed out, returned 44 chars of speech, and left
hundreds of chars of chunks + tool_call events + prompt_complete in the pipe.
From that point on, every response was shifted one behind.

**Fix (applied in asdaaas.py):** `drain_stale_frames()` is called before each
new prompt. It reads any buffered frames with a 50ms timeout (instant if data
is present, 50ms cost if pipe is clean). Recovered speech chunks are delivered
to the outbox rather than silently dropped. Fragments are logged and discarded.

**Diagnosis steps:**
1. Check asdaaas log for `agent_think=0ms` on multiple consecutive messages
2. Compare asdaaas log response content with session `updates.jsonl` — if they
   differ, the pipe is desynced
3. Look for `streaming=113000ms+` or `total=120000ms+` entries — these are
   timeout events that may have caused the desync
4. Look for `auto_compact_started` / `auto_compact_completed` in the log near
   the first desynced message
5. After deploying the fix, look for `[asdaaas] DRAIN:` log lines — these
   confirm stale frames were caught and handled

### Localmail doorbell not delivered
1. Check localmail log: `tail -20 /tmp/localmail_adapter.log`
2. Check if target is detected as asdaaas agent: health file must be < 1 hour old
3. Check doorbell directory: `ls ~/agents/Q/asdaaas/doorbells/`
4. Check asdaaas log for doorbell pickup: `grep -i doorbell /tmp/asdaaas_q.log | tail -5`

### OOM / memory issues
1. Check RSS: `ps aux --sort=-rss | grep grok | head -5`
2. Check updates.jsonl sizes: `du -sh ~/.grok/sessions/*/*/updates.jsonl 2>/dev/null`
3. **NEVER use `is_background: true` for long-running processes** — causes updates.jsonl bloat
4. Always use `setsid nohup` via launch scripts

### Agent session corruption
1. Check session directory exists: `ls ~/.grok/sessions/%2Fhome%2Feric/019d1ec1*/`
2. CWD must match session creation path (URL-encoded)
3. If session won't load, create new session (omit `--session` flag)

## Version Check

```bash
bash scripts/asdaaas_version.sh   # shows RUNNING vs HEAD commit (NOTE: script references mikeyv-infra, needs update to agent-abide)
grok --version                # grok binary version
```
