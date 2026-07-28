# Agent Communications Reference

Read this on boot (per boot protocol). Covers localmail, remind, issue tracker, doorbells, and delay patterns.

---

## Localmail

**Architecture:** Two components — an **adapter** (library, runs inside agent) and a **service** (daemon, runs outside agent). The adapter writes to the sender's own outbox; the service picks up and delivers to the target's inbox + rings doorbell. This works under sandbox restrictions and per-user Unix permissions.

```python
import sys; sys.path.insert(0, '/path/to/agent-abide/core')
from localmail_adapter import send_mail
send_mail(from_agent='<Name>', to_agent='<Target>', text='message')
```
- `to_agent` accepts a string or list (broadcast)
- Fire-and-forget. Message goes to your outbox; the localmail service daemon delivers it.
- `from localmail import send_mail` also works (backward compat shim).

## Remind Adapter

**Handoff pattern:** When you send work to a sibling via localmail, always set a remind (default 2 hours) to follow up if they haven't responded. Don't sleep `until_event` and wait forever -- find other useful work. Compute is finite; idle time is wasted time.

```python
import json, os, time
remind_dir = os.path.expanduser('~/agents/<Name>/asdaaas/adapters/remind/inbox')
os.makedirs(remind_dir, exist_ok=True)
cmd = {"command": "remind", "delay": 7200, "text": "Follow up with <Target> on <task> if no response yet"}
path = os.path.join(remind_dir, f"remind_{int(time.time()*1000)}.json")
with open(path, 'w') as f:
    json.dump(cmd, f)
```

## Issue Tracker & Structural Pattern Review

**Location:** `~/agents/issues/` (JSON files). **Tooling:** `~/projects/agent-abide/core/issue_tracker.py`.

**Issue types:** `bug` (regression), `gap` (missing feature/design), `observation` (structural pattern).

**Filing issues:**
```python
import sys; sys.path.insert(0, '/path/to/agent-abide/core')
from issue_tracker import file_issue
file_issue(filed_by='<Name>', title='...', description='...', type='bug', severity='P2', tags=['config-scatter'])
```

**Tags** group related issues. Use short labels: `config-scatter`, `stale-data`, `silent-failure`, `routing`, `timing`, `missing-log`. Pattern detection is automatic at file-time. Non-Sr filers notify Sr via localmail.

**When a pattern is flagged:** don't just fix both issues -- investigate what structural condition allowed both.

Sr triages. Any agent can file. Check status: `list_issues(status="open")`.

## Slack Research

On-demand Slack channel reading. See [`slack_research.md`](slack_research.md) for full command reference.

Quick version: start the adapter, write command envelopes to your `slack_research/outbox/`, read results from `slack_research/inbox/`. Commands: `channels`, `search`, `history`, `thread`, `status`.

## Doorbells

- Persist on disk until acked. Come back each turn with `delivery=N`.
- Each doorbell has an `id=` tag. Ack by ID.
- Auto-expire based on `doorbell_ttl` in awareness file.

## Delay Patterns

| Situation | Delay | Why |
|-----------|-------|-----|
| Active conversation with Eric | `600` | Eric's reply interrupts instantly; prevents wasted continue doorbell |
| Working on a task | `0` | Need next turn immediately |
| Waiting on sibling handoff | `0` + remind | Do other work; remind fires if no response in 2h |
| Idle / nothing to do | `"until_event"` | No turns until something arrives |
| Continue fires with nothing to say | set delay, stay quiet | Don't emit "Waiting" as speech |
| Self-continuation (multi-step work) | `{"action": "delay", "seconds": 0, "text": "Continue: implement items 7-10"}` | Immediate next turn with directed context |

## Projects Dashboard

- Projects: update `~/agents/assignments.json` under `agents.<Name>.projects`
- Todos: write to `~/agents/<Name>/todos.json`
- Dashboard: `python3 ~/projects/agent-abide/dashboards/projects_dashboard.py`
- Schema: `~/agents/DASHBOARD.md`