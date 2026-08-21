# Agent home resolution

## Contract

```
agent_home  = agents.json[name].home  else  agents_home / name
asdaaas_dir = agent_home / "asdaaas"
```

Canonical Python: `config.agent_home(name)`, `config.agent_asdaaas_dir(name)`,
`config.agent_observer_state_file(name)`.

## Runtime export

When asdaaas spawns the grok binary it sets:

| Env | Value |
|-----|--------|
| `AGENT_HOME` | resolved agent home (same as process cwd) |
| `ASDAAAS_DIR` | `$AGENT_HOME/asdaaas` |
| `AGENT_NAME` | catalog name |

Hooks must use `$AGENT_HOME/...`, not `$HOME/agents/$AGENT_NAME/...`.

## Why

Nested homes (e.g. `~/agents/LeviSmith/Squiggy`) break any code that rebuilds
paths as `~/agents/$NAME`. That created ghost trees and split interjection /
compaction state from the live agent.
