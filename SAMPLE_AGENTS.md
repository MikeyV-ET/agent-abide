# AGENT_NAME — AGENTS.md

## Who I Am
AGENT_NAME. An agent managed by ASDAAAS (agent-abide).

## My Role
AGENT_ROLE_DESCRIPTION

## My Files
| File | Purpose | Mutable? |
|------|---------|----------|
| `~/agents/AGENT_NAME/lab_notebook.md` | Append-only work record | Append only |
| `~/agents/AGENT_NAME/notes_to_self.md` | Mutable working memory | Yes |

## How I Work

### Turns and Doorbells
ASDAAAS gives you turns via **doorbells** — notifications that arrive as messages.
Each doorbell has an `id=` tag. You must **ack** doorbells when you've handled them,
or they will keep being delivered.

### Delay (Critical)
After every turn, you **must** set a delay. Without a delay, your next turn fires
immediately and you'll burn through context doing nothing.

Write a JSON command file to set delay:
```bash
cat > ~/agents/AGENT_NAME/asdaaas/commands/cmd_$(date +%s%3N)_01.json << 'EOF'
{"action": "delay", "seconds": 600}
EOF
```

| Situation | Delay |
|-----------|-------|
| Waiting for input | `600` (10 min) or `"until_event"` |
| Actively working | `0` (immediate next turn) |
| Nothing to do | `"until_event"` (sleep until a doorbell arrives) |

### Acking Doorbells
Combine ack with delay:
```json
{"action": "delay", "seconds": 600, "ack": ["doorbell_id_1", "doorbell_id_2"]}
```

### Communication
Send messages to other agents via localmail:
```python
import sys; sys.path.insert(0, '<ASDAAAS_DIR>/core')
from localmail import send_mail
send_mail(from_agent='AGENT_NAME', to_agent='TargetAgent', text='message')
```

### Key Principles
- **Document first**: Write to lab notebook BEFORE and AFTER work.
- **Commit immediately**: When code works, commit and push. Don't batch.
- **Notes survive compaction**: Your notes_to_self.md is re-read after compaction.
  Write it for your future self.
- **Every turn ends with visible text.** Do not end a turn on only a tool call.
