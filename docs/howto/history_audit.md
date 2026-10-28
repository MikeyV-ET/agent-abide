# History audit (Grok)

Backend-specific. First tool: walk **current session** `updates.jsonl` and check house **`history/hot.jsonl`** has the same coalesced user/assistant text (15% char slack, or one side contained in the other).

```bash
cd ~/projects/agent-abide-dev
PYTHONPATH=core python3 scripts/audit_grok_history.py --agent Trip-G
PYTHONPATH=core python3 scripts/audit_grok_history.py --agent Squiggy
```

`updates.jsonl` is the grok flight recorder. `hot.jsonl` is aa-dev TUI SoR. This is a compare, not ingest. Claude jsonl is a later backend.
