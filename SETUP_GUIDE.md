# Agent Setup Guide for agent-abide

**This document is for you, the agent.** A user has pointed you at this repo and asked you to set up agent-abide. Follow these steps. Ask the user questions when noted. Do not skip steps.

## What is agent-abide?

agent-abide (ASDAAAS) is persistent agent infrastructure. It keeps agents running continuously — managing turns, doorbells, delays, gaze (where output goes), and awareness (what you hear). Once set up, agents persist across sessions and survive context compaction.

You are about to set up this system. **You will register yourself as the first agent** — once ASDAAAS is running, it will manage your turns instead of the user's CLI.

## Step 1: Check prerequisites

Verify these are installed. If anything is missing, tell the user exactly what to install.

```bash
python3 --version    # Need 3.11+
node --version       # Need 20+
npm --version
which grok           # Need grok CLI (npm install -g @xai-official/grok)
```

Check Python packages:
```bash
pip install -r requirements.txt
```

Check grok authentication:
```bash
ls ~/.grok/auth.json    # Should exist
```

If auth.json doesn't exist, tell the user:
> "You need to authenticate grok. Please run: `grok login --device-auth` and follow the prompts. Let me know when you're done."

## Step 2: Ask the user what they want

Ask the user these questions:

1. **"What do you want your agents to do?"** — Suggest a default team of three:
   - **Coder** — writes and fixes code
   - **Tester** — writes tests and validates changes
   - **Reviewer** — reviews code for correctness and quality

   The user might want a single agent, different roles, or different names. Adapt accordingly.

2. **"Where should agent home directories live?"** — Default: `~/agents`

3. **"What should I call myself as your first agent?"** — You will register yourself. Suggest a name based on context (e.g., the Coder role if they chose the default team).

## Step 3: Configure agents.json

```bash
cp agents.json.example agents.json
```

Edit `agents.json`:
- Set `asdaaas_dir` to the absolute path of this repo clone
- Set `agents_dir` to where agent homes will live (from Step 2)
- Set `running_agents_file` to `<agents_dir>/running_agents.json`
- Set `grok_binary` to the output of `which grok`
- Leave `agents` section empty (Step 4 will populate it)

## Step 4: Create agents

For each agent the user wants:

```bash
bash scripts/setup_agent.sh <AgentName> <agents_dir>
```

Then add each agent to the `"agents"` section of `agents.json`:

```json
"<AgentName>": {
    "session": "",
    "home": "<agents_dir>/<AgentName>",
    "observer_enabled": true,
    "interjection_enabled": false
}
```

**Include yourself.** You are the first agent. Create your own directory and register yourself.

### Customize each agent's AGENTS.md

Edit `<agents_dir>/<AgentName>/AGENTS.md` for each agent. This file controls the agent's behavior. At minimum it must contain:

- **Who the agent is** (name, role)
- **How delays work** (critical — without setting delay, agents burn through context):

```
After every turn, set a delay by writing a command file:
cat > ~/agents/<Name>/asdaaas/commands/cmd_$(date +%s%3N)_01.json << 'EOF'
{"action": "delay", "seconds": 600}
EOF

Use seconds: 0 for immediate next turn, 600 for waiting, "until_event" for idle.
```

- **How to ack doorbells** (messages arrive with IDs; ack them or they repeat)
- **How to communicate** with other agents (localmail)

The file `SAMPLE_AGENTS.md` in this repo has a complete template. Role-specific descriptions are in `templates/` (coder.md, tester.md, reviewer.md).

Read `docs/howto/commands.md` and `docs/howto/comms.md` for full reference — these are essential for agent operation.

## Step 5: Launch

```bash
bash scripts/launch_asdaaas.sh --wait
```

This launches all registered agents. The `--wait` flag shows startup progress. Each agent takes 30-60 seconds to reach Ready.

## Step 6: Verify

```bash
bash scripts/check_agent.sh <AgentName>     # Should show UP / working
bash scripts/smoke_test.sh                   # 57 checks, all should pass
```

## Step 7: Brief the user

Tell the user:

1. **How to talk to agents:** `bash scripts/launch_tui.sh` opens the TUI (terminal UI). It will ask for their name on first launch. They can type messages and agents respond.

2. **How to check status:** `bash scripts/check_agent.sh <Name>` shows if an agent is running.

3. **How to stop:** `bash scripts/stop_asdaaas.sh` stops everything. `bash scripts/stop_agent.sh <Name>` stops one agent.

4. **What to expect:** Agents are now persistent. They process turns continuously, communicate with each other via localmail, and survive context compaction by re-reading their lab notebooks and notes.

5. **The TUI command they'll use most:**
```bash
bash scripts/launch_tui.sh
```

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `PermissionError` on launch | `asdaaas_dir` in agents.json is wrong — set it to this repo's path |
| `check_agent.sh` says "no health file" | Agent is still starting (30-60s). Wait and retry |
| Agent exits immediately | Check the log: `cat /tmp/asdaaas_<name>.log` |
| Agent spins without stopping | AGENTS.md is missing delay instructions |
| grok not authenticated | User must run `grok login --device-auth` |
