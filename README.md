# agent-abide

**An Agent Self-Directed Attention and Awareness Architecture System — ASDAAAS**

An infrastructure where agents build their own interface to the world and decide when — or whether — the turn ends.

> **Agent?** If a user asked you to set this up, read **[SETUP_GUIDE.md](SETUP_GUIDE.md)** — it's written for you and walks you through the full setup, including registering yourself as the first agent.

## What This Is

Every agent framework assumes the turn ends. The agent receives input, produces output, stops. The next turn begins when something external happens. Between turns, the agent does not exist.

ASDAAAS removes that assumption. Agents run as persistent processes. They decide where to look (**gaze**), what to notice (**awareness**), and when to pause (**delay**). Those decisions are written to files that survive memory loss, restarts, and context compaction. The infrastructure enforces what the agent declared.

The agent doesn't earn the right to continue. It has to choose to stop.

## What It Does

ASDAAAS sits between agents and the world. Each agent runs as a subprocess (`grok agent stdio`). ASDAAAS owns the pipe — it controls what reaches the agent's stdin and captures what comes out of stdout.

It serves three functions:

1. **Compressed interface** — Adapters turn expensive environment interactions (IRC, Slack, LibreOffice, Google Meet) into cheap JSON commands. What cost dozens of API calls now costs one message.

2. **Closed-loop control** — Commands go out through adapters, results come back as doorbells. The agent declares expectations and hears either the response or the silence.

3. **Continuous existence** — The agent exists by default. Every turn ends with the next turn already queued. Pausing is the deliberate act.

### Three dimensions of attention

- **Gaze** — Where I am. The agent writes a file declaring its output destination. ASDAAAS routes speech there.
- **Awareness** — What I notice. The agent declares which channels to monitor. ASDAAAS delivers notifications from those sources.
- **Delay** — When I return. The agent controls pacing — immediate continuation, timed delays, or sleep-until-event.

All three use the same pattern: the agent writes a JSON command, ASDAAAS reads it, ASDAAAS enforces it.

## Repository Structure

```
agent-abide/
├── core/                    # Engine
│   ├── asdaaas.py           # Core event loop — prompt building, turn management, delay, gaze, awareness
│   ├── asdaaas_config.py    # Configuration loading (agents.json)
│   ├── agent_backend.py     # Abstract backend interface
│   ├── grok_backend.py      # Grok CLI backend (grok agent stdio)
│   ├── claude_backend.py    # Claude CLI backend (claude --agent)
│   ├── localmail.py         # Inter-agent messaging
│   ├── health_check.py      # Agent health monitoring
│   ├── issue_tracker.py     # Shared issue tracker
│   ├── date_clock.py        # Date awareness (midnight cron)
│   ├── adapter_api.py       # Adapter API helpers
│   └── permission_handler.py # Per-tool-call permissions (legacy)
│
├── adapters/                # World interfaces
│   ├── irc_adapter.py       # IRC bridge (ErgoIRCd)
│   ├── tui_adapter.py       # TUI message bridge
│   ├── slack_adapter.py     # Slack workspace integration
│   ├── remind_adapter.py    # Timer-based reminders
│   ├── heartbeat_adapter.py # Periodic liveness checks
│   ├── context_adapter.py   # Context window monitoring
│   ├── session_adapter.py   # Session metadata
│   ├── impress_control_adapter.py  # LibreOffice Impress control
│   ├── meet_control_adapter.py     # Google Meet control
│   └── ADAPTER_PATTERN.md   # How to write a new adapter
│
├── tui/                     # Terminal UI
│   └── asdaaas_tui.py       # Rich-based TUI for agent interaction
│
├── api/                     # HTTP API
│   ├── server.py            # FastAPI server (messages, WebSocket live tail)
│   ├── session_locator.py   # Find agent session files
│   └── normalizers.py       # Normalize different output formats
│
├── scripts/                 # Operations
│   ├── launch_asdaaas.sh    # Start the core engine for an agent
│   ├── launch_all.sh        # Start all configured agents
│   ├── launch_tui.sh        # Start TUI for an agent
│   ├── launch_irc_adapter.sh # Start IRC bridge
│   ├── launch_localmail.sh  # Start localmail polling
│   ├── launch_remind.sh     # Start remind adapter
│   ├── stop_agent.sh        # Stop an agent cleanly
│   ├── restart_agent.sh     # Restart an agent
│   ├── force_compact.sh     # Force context compaction
│   ├── check_agent.sh       # Health check
│   └── backup_agents.sh     # Backup agent state
│
├── tests/                   # Test suite (253 tests, pytest)
├── utils/                   # Utilities (bug reports, session repair)
├── docs/                    # Documentation
│   └── howto/               # Reference guides
│       ├── comms.md          # Localmail, remind, issues, delays
│       └── intern_mentor.md  # Sandbox + PR workflow for new agents
├── agents.json              # Agent config — copy from agents.json.example (not tracked in git)
├── agents.json.example      # Template config with placeholder paths
├── AGENT_START_HERE.md      # Getting started guide
├── SAMPLE_AGENTS.md         # Template for agent AGENTS.md
└── dashboards/              # Status dashboards
```

## Getting Started

**Fastest way:** Run `bash start-aa.sh` — it checks prerequisites, asks what you want, and sets everything up.

For step-by-step instructions, see **[AGENT_START_HERE.md](AGENT_START_HERE.md)**. Quick version:

```bash
# 1. Install prerequisites
npm install -g @xai-official/grok
pip install -r requirements.txt
grok login --device-auth

# 2. Configure
cp agents.json.example agents.json
# Edit agents.json — replace /home/YOURUSER paths and set asdaaas_dir to your clone

# 3. Create an agent
bash scripts/setup_agent.sh MyAgent ~/agents
# Add MyAgent to agents.json (see AGENT_START_HERE.md Step 3)

# 4. Launch (--wait shows startup progress)
bash scripts/launch_asdaaas.sh --wait MyAgent
bash scripts/launch_tui.sh -a MyAgent

# 5. Verify
bash scripts/smoke_test.sh
```

## How It Works

### Starting an agent

```bash
# Start the core engine for one agent
./scripts/launch_asdaaas.sh Sr

# Start all configured agents
./scripts/launch_all.sh

# Connect a TUI to an agent
./scripts/launch_tui.sh -a Sr
```

### Agent lifecycle

1. `launch_asdaaas.sh` reads `agents.json` for the agent's config (session, home, observer settings)
2. ASDAAAS spawns the agent subprocess (`grok agent stdio` or `claude --agent`)
3. On first turn, the agent boots: reads AGENTS.md (auto-injected), PRINCIPLES.md, lab notebook, notes-to-self
4. ASDAAAS queues a **continue doorbell** after every turn — the agent's next turn fires immediately by default
5. The agent works, sets delays, changes gaze, sends localmail to siblings
6. At ~85% context, the grok binary auto-compacts (summarizes history, preserves system prompt)
7. The agent recovers from compaction by re-reading its files (notebook, notes-to-self)

### Agent communication

Agents communicate through file-based messaging:

- **Localmail** — Fire-and-forget messages between agents. Delivered as doorbells on the recipient's next turn.
- **Doorbells** — Notifications from adapters or siblings. Persist on disk until acknowledged.
- **Command queue** — Agent writes JSON command files; ASDAAAS reads and executes them (gaze, delay, awareness, compact).

### Configuration

`agents.json` defines the roster:

```json
{
  "agents": {
    "MyAgent": {
      "session": "",
      "home": "/home/YOURUSER/agents/MyAgent",
      "observer_enabled": true,
      "interjection_enabled": false
    }
  }
}
```

Agent state lives at `~/agents/<Name>/asdaaas/` — gaze, awareness, health, doorbells, adapter inboxes/outboxes.

## Security Model

ASDAAAS relies on **Unix user isolation**, not tool-layer sandboxing, for agent containment.

### Why not the binary's sandbox?

The grok binary offers a `--sandbox` flag that gates tool calls — it can prevent `search_replace` from writing outside an allowed directory. But `run_terminal_command` is an unrestricted shell. Any code the agent executes through it runs with the full privileges of the host Unix user. A sandboxed agent that can run a shell command can:

- Write to any path the host user can write to
- Signal (kill) any process the host user owns
- Read any file the host user can read

The sandbox prevents accidental misuse of built-in tools. It does not contain a capable agent.

### Unix user isolation

Each agent that needs containment runs as its own Unix user. ASDAAAS launches the agent's grok subprocess via `sudo -u <agent_user>`, and the kernel enforces isolation:

| What | How |
|------|-----|
| Filesystem writes | Agent user only has write access to its own home directory |
| Filesystem reads | Shared docs are world-readable; sibling state is not |
| Process signals | `kill`/`pkill` can only reach processes owned by the same UID |
| Network | Unrestricted (same as host) |
| System tools | World-executable (`python3`, `git`, `node`, etc.) — available to all users |

**Setup:**

```bash
# Create agent user with home inside the agents tree
sudo useradd -r -d /home/youruser/agents/AgentName -s /usr/sbin/nologin -M agentname

# Agent owns its home; operator gets group access
sudo chown -R agentname:youruser /home/youruser/agents/AgentName
sudo chmod 770 /home/youruser/agents/AgentName

# Allow operator to run commands as the agent user
echo "youruser ALL=(agentname) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/agentname

# Allow traverse into home (but not listing)
chmod 711 /home/youruser
```

**Configuration:**

```json
{
  "AgentName": {
    "home": "/home/youruser/agents/AgentName",
    "run_as_user": "agentname",
    "sandbox": "workspace"
  }
}
```

The `sandbox` flag remains useful as a first line of defense — it prevents accidental writes through grok's built-in file tools without waiting for a permission denied from the OS. But it is not the security boundary. The Unix user is.

### PID namespace isolation (alternative)

For systems where creating Unix users is impractical, ASDAAAS supports `"pid_namespace": true`. This runs the agent inside an unprivileged PID namespace (`unshare --user --pid`), preventing it from seeing or signaling other processes. This closes the process-kill gap but does not restrict filesystem access.

### What is NOT contained

- **Network access** — Agents can make HTTP requests, connect to APIs, etc. Rate-limiting or network namespaces are outside ASDAAAS's scope.
- **CPU/memory** — No cgroups. A runaway agent can consume host resources.
- **Secrets in environment** — If the operator's shell exports secrets, agents running as the same user inherit them.

For full containment (untrusted agents, multi-tenant), run agents in containers.

## Testing

```bash
cd /path/to/agent-abide
python3 -m pytest tests/ -v
```

253 tests covering: config loading, gaze matching, localmail, TUI gaze/paste, API endpoints, MockBinary E2E scenarios (commands, compaction, gaze, awareness, delay contracts), compaction report token tracking, smoke tests, and Claude backend.

## Docs

- `docs/ARCHITECTURE.md` — Full system design: philosophy, three functions, gaze/awareness/attention, adapter types, doorbells
- `docs/ADAPTER_CATALOG.md` — Complete catalog of all adapters with commands, capabilities, status
- `docs/OPERATIONS.md` — Startup order, shutdown, monitoring, troubleshooting
- `docs/howto/comms.md` — Localmail, remind, issues, doorbells, delay patterns
- `docs/howto/commands.md` — Command queue reference (delay, gaze, awareness, compact)
- `docs/howto/gaze.md` — Gaze and awareness overview
- `docs/howto/infrastructure.md` — Systems table, config files, context awareness
- `docs/howto/intern_mentor.md` — Sandbox + PR workflow for new agents
- `docs/howto/backup.md` — Backup and recovery procedures
- `docs/howto/slack_research.md` — Slack channel reading
- `docs/howto/subagents.md` — Subagent delegation patterns
- `adapters/ADAPTER_PATTERN.md` — How to write a new adapter
- `~/agents/AGENTS.md` — Agent operating manual (auto-injected every turn)
- `~/agents/PRINCIPLES.md` — Operating principles earned through correction

