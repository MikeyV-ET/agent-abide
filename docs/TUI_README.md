# asdaaas TUI

Full-screen terminal interface for asdaaas agent sessions. Built with [Textual](https://textual.textualize.io/).

## Quick Start

```bash
# Launch for a specific agent (required)
bash launch_tui.sh --agent Trip

# With operator name (skip the "Who are you?" prompt)
bash launch_tui.sh --agent Sr --operator eric

# Light mode
bash launch_tui.sh --agent Trip --light

# Replay last 50 events (fast startup for large sessions)
bash launch_tui.sh --agent Jr --replay --tail 50
```

## Requirements

- Python 3.10+
- `textual` (TUI framework)
- `rich` (terminal rendering, installed with textual)

Install:
```bash
pip install textual
```

## Features

- **Multi-agent tabs** -- switch between agents with Ctrl+N or click tab bar
- **Live streaming** -- agent output appears in real-time via updates.jsonl tailing
- **Tool call display** -- collapsible panels showing tool inputs/outputs (click to expand)
- **Gaze selector** -- Ctrl+G to change where the agent's output is directed
- **Slash commands** -- `/gaze`, `/awareness`, `/mail`, `/status` in the input box
- **Thinking blocks** -- F1 toggles visibility of agent thinking/reasoning
- **Persistence panel** -- F2 shows health, notebook, git, compaction status
- **Turn separators** -- visual dividers between agent turns with turn number and timestamp
- **History loading** -- PageUp loads older events from the session
- **Ctrl+E edit mode** -- toggle between Enter-sends and Enter-inserts-newline
- **Plan/todo display** -- agent todo lists rendered inline
- **Alert system** -- system alerts (errors, warnings, info) displayed in the conversation
- **Dark/Light themes** -- Gruvbox dark (default) or light (`--light`)

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| Enter | Send message |
| Ctrl+E | Toggle edit mode (Enter = newline) |
| Ctrl+Enter | Insert newline (when edit mode off) |
| Ctrl+C | Interrupt running agent |
| Ctrl+G | Open gaze selector |
| Ctrl+N | Switch to next agent tab |
| Ctrl+L | Clear screen |
| Ctrl+Q | Quit |
| F1 | Toggle thinking blocks |
| F2 | Persistence panel |
| PageUp | Load older history |
| Home | Scroll to top |
| End | Scroll to bottom |
| Escape | Dismiss overlay |

## Slash Commands

Type these in the input box:

| Command | Description |
|---------|-------------|
| `/gaze <adapter> [target]` | Set agent gaze (e.g. `/gaze irc #channel`) |
| `/awareness add <channel>` | Add background awareness channel |
| `/awareness remove <channel>` | Remove awareness channel |
| `/mail <agent> <message>` | Send localmail to another agent |
| `/status` | Show agent status summary |

## CLI Options

```
--agent, -a NAME      Agent name (required)
--agents-home DIR     Agents home directory (default: ~/agents)
--updates, -u PATH    Path to updates.jsonl (auto-detected if not specified)
--replay, -r          Replay session from beginning
--tail, -t N          Replay last N events only
--operator, -o NAME   Operator name (skip prompt)
--sessions-dir DIR    Override grok sessions directory
--debug-log PATH      Write dispatch debug log
--light               Use Gruvbox light color scheme
```

## Architecture

```
asdaaas_tui.py (this file)
    |
    |-- reads updates.jsonl (agent output stream)
    |-- writes to tui adapter inbox (user messages)
    |-- reads/writes gaze.json, awareness.json via command queue
    |
tui_adapter.py (separate process)
    |-- bridges TUI messages to/from asdaaas controller
```

The TUI reads the agent's `updates.jsonl` file directly for output and writes user messages as JSON files to the TUI adapter inbox (`~/agents/<name>/asdaaas/adapters/tui/inbox/`). The TUI adapter process picks these up and delivers them to the asdaaas controller.

## File Locations

- TUI code: `~/projects/agent-abide/asdaaas_tui.py`
- TUI adapter: `~/projects/agent-abide/tui_adapter.py`
- Agent state: `~/agents/<name>/asdaaas/`
- Session data: `~/.grok/sessions/`
