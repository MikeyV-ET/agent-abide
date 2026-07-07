# Getting Started with agent-abide

Set up your first agent in 5 steps.

## Prerequisites

- **Python 3.11+**
- **Node.js 20+** and **npm**
- **grok CLI**: `npm install -g @xai-official/grok`
- **grok auth**: `grok login --device-auth` (or set `XAI_API_KEY` env var)
- **Python packages**: `pip install requests websockets websocket-client textual rich`

## Step 1: Configure agents.json

```bash
cp agents.json.example agents.json
```

Edit `agents.json` — replace all `/home/YOURUSER` paths with your actual home directory:

```json
{
  "settings": {
    "log_dir": "/tmp",
    "running_agents_file": "/home/YOURUSER/agents/running_agents.json",
    "timezone": "America/Los_Angeles"
  },
  "agents": {}
}
```

## Step 2: Create an agent

```bash
bash scripts/setup_agent.sh MyAgent ~/agents
```

This creates the directory tree at `~/agents/MyAgent/` with all required files:
awareness, gaze, lab notebook, notes, and adapter directories.

## Step 3: Register the agent in agents.json

Add your agent to the `"agents"` section of `agents.json`:

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

- `session`: leave empty for a new agent (a session ID is assigned on first launch)
- `home`: path to the agent directory created in Step 2
- `observer_enabled`: enables the binary state observer (recommended)
- `interjection_enabled`: enables mid-turn interjection (optional)

## Step 4: Launch

```bash
bash scripts/launch_asdaaas.sh MyAgent
```

Check that it started:

```bash
bash scripts/check_agent.sh MyAgent
```

You should see `UP` with a status of `ready` or `working`.

## Step 5: Talk to your agent

```bash
bash scripts/launch_tui.sh -a MyAgent
```

Type a message and press Enter. The agent responds in the TUI.

## Verify your setup

Run the smoke test to check everything is wired correctly:

```bash
bash scripts/smoke_test.sh
```

All checks should pass.

## What's next

- **Give your agent identity**: create `~/agents/MyAgent/AGENTS.md` with the agent's name, role, and instructions. See `SAMPLE_AGENTS.md` for a template.
- **Add adapters**: launch localmail (`scripts/launch_localmail.sh`), IRC (`scripts/launch_irc_adapter.sh`), or other adapters for multi-agent communication.
- **Read the architecture**: `docs/ARCHITECTURE.md` explains gaze, awareness, delay, and how adapters work.
- **Operations guide**: `docs/OPERATIONS.md` covers startup order, monitoring, and troubleshooting.

## Stopping an agent

```bash
bash scripts/stop_agent.sh MyAgent
```

Or stop everything:

```bash
bash scripts/stop_asdaaas.sh
```
