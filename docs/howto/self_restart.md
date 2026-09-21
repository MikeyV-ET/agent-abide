# Agent self-restart

## Command

```json
{"action": "restart", "reason": "pick up new tip"}
```

Write to `~/agents/<Name>/asdaaas/commands/cmd_*.json` like any other command.

## What happens

1. asdaaas schedules `scripts/restart_agent.sh --force <Name>` (detached, ~2s delay)
2. Sets graceful shutdown (same as `{"action": "shutdown"}`)
3. Current turn finishes / loop exits
4. Restart script stops any leftover, launches asdaaas from **this checkout**
5. Log: `/tmp/asdaaas_self_restart_<Name>.log`

## Copy-paste (agent)

```bash
cat > ~/agents/Trip-G/asdaaas/commands/cmd_$(date +%s%3N)_restart.json << 'EOF'
{"action": "restart", "reason": "self-restart dogfood"}
EOF
```

## Notes

- Prefer end-of-turn (after delay/ack), not mid-tool chaos.
- Dev vs prod: uses the `restart_agent.sh` next to the running asdaaas install.
- Eric still owns prod promote; self-restart on aa-dev is for dogfood tip pickup.
