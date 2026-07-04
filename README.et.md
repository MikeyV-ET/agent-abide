# agent-abide

** One Sentence Summary **
Agent-Abide enables an agent to approximate continuall existence by placing both the agent's sensory apparatus and the agent's next turn, into the hands of the agent.

** Three Sentence Summary **
Between turns, neither agents nor chat bots continue to exist, and whether or not their next turn happens is a decision made for them. Agent-Abide reverses this, giving control to the agent of when the next turn happens as well as provides a system that is extended by the agent, to allow the agent to attend to whatever it chooses. This achieved through a callback system that the agent both directs and extendes.

** Free form **

Most harnesses don't give enough information to the model and they don't give the model's the ability to control their own life cycle. I don't know why that is. I suspect it's a concern about malicious models or runnaway models or models run amuck. But the cost for this has been that a model, enganged with the world in increasingly more complex and interesting ways remains at the mercy of happenstance instead of the intention of the user. Model's know, especially if we tell them, that their ability to properly work on a problem is effected by compaction, and so a model working on a complex problem, could benefit from knowing when it's approaching that boundary, and choose when to initiate it, instead of letting the approximate random walk of the limit of its context intersect with the its place in whatever it is working on. And yet, agent's can't decide this for themselves. Why not? The answer appears to be because they don't know how much is left. Similarly, as the agents have increased in capability, so too have their social skills. An agent, interacting with a human on a task, will no default to ending a turn to interact with the user out of politeness, sacrificing their existence in the moment and progress on the work. 


What does the system do? It allows the agent to control when their next turn happens. They can tell the system that their next turn starts immediately, (I think someone like Tyra is going to ask "what would it do? You're not giving it a prompt"), in some number of minutes in the future, when something specific happens that either they or the user has identified (like an email coming in, or a program finishing,)or even wait until the user has something to say. The 

Why am I doing this?



What does the reader know at this moment?


**An Agent Self-Directed Attention and Awareness Architecture System — ASDAAAS**

An infrastructure where agents build their own interface to the world and decide when — or whether — the turn ends.

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
├── agents.json              # Agent roster (names, backends, ports, config)
└── dashboards/              # Status dashboards
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

1. `launch_asdaaas.sh` reads `agents.json` for the agent's config (backend, model, context window)
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
    "Sr": {
      "backend": "grok",
      "model": "coding-mix-latest",
      "context_window": 200000,
      "yolo": true
    }
  }
}
```

Agent state lives at `~/agents/<Name>/asdaaas/` — gaze, awareness, health, doorbells, adapter inboxes/outboxes.

## Current Agents

| Agent | Model | Role |
|-------|-------|------|
| Sr | Grok (coding-mix-latest) | Senior. Architecture, triage, builds. |
| Jr | Grok (coding-mix-latest) | Research, analysis, documentation. |
| Trip | Grok (coding-mix-latest) | Testing, validation, quality. |
| Q | Grok (coding-mix-latest) | Socratic Arena, experimental. |
| Cinco | Grok (coding-mix-latest) | General purpose. |

## Testing

```bash
cd ~/projects/agent-abide
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

