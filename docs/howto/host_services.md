# Host services under agent control (without polluting updates.jsonl)

## The problem

If an agent runs `bash scripts/launch_localmail.sh` **as a model tool**, the
shell's stdout/stderr is captured into the binary session stream
(`updates.jsonl` / Claude session jsonl). That:

- pollutes history / hot / TUI
- can trigger interjection hooks on noise
- couples infra lifecycle to a single agent's transcript

## The rule

**Model tools never own long-lived host daemons.**  
**asdaaas (host) starts them, fully detached, logs to `/tmp` only.**

Launch scripts already do the right isolation:

```bash
setsid nohup python3 -u … > /tmp/<service>_adapter.log 2>&1 &
```

## Agent API (command queue)

```json
{"action": "ensure_service", "service": "remind", "op": "ensure"}
```

| field | values |
|-------|--------|
| `service` | `localmail` \| `remind` \| `heartbeat` |
| `op` | `ensure` (default) \| `status` \| `stop` |

Write to `asdaaas/commands/cmd_*.json` like delay/restart. asdaaas runs the
matching `scripts/launch_*.sh` in a **new session**, stdin/stdout/stderr
discarded (script logs to `/tmp`).

### Copy-paste

```bash
cat > ~/agents/Trip-G/asdaaas/commands/cmd_$(date +%s%3N)_svc.json << 'EOF'
{"action": "ensure_service", "service": "remind", "op": "ensure"}
EOF
```

## What NOT to do

```bash
# BAD — tool stdout → updates.jsonl
bash ~/projects/agent-abide-dev/scripts/launch_remind.sh
```

```bash
# OK only if fully detached AND you never read the output back into the tool
setsid nohup bash … > /tmp/x.log 2>&1 &
# Still prefer ensure_service so asdaaas owns lifecycle.
```

## Remind vs sleep

- **remind adapter**: durable, survives agent restart, wakes via doorbell
- **detached sleep+restart**: backup wall-clock; does not need Trip-G awake
- Prefer both for critical wakes (Astro session_limit 01:40 PT pattern)

## Status

`{"action":"ensure_service","service":"remind","op":"status"}` → control line + log.
