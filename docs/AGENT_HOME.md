# Agent home resolution

## Contract

```
agent_home  = agents.json[name].home  else  agents_home / name
asdaaas_dir = agent_home / "asdaaas"
```

Canonical Python:
- `config.agent_home(name)`
- `config.agent_asdaaas_dir(name)`
- `config.agent_observer_state_file(name)`
- `config.resolve_asdaaas_dir(name, agents_home_override=None)` — production nested home;
  when `agents_home_override` differs from `config.agents_home` (tests monkeypatch
  `AGENTS_HOME_DIR`), uses flat `override/name/asdaaas`.

## Runtime export (grok binary env)

| Env | Value |
|-----|--------|
| `AGENT_HOME` | resolved agent home (= process cwd) |
| `ASDAAAS_DIR` | `$AGENT_HOME/asdaaas` |
| `AGENT_NAME` | catalog name |

Hooks must use `$AGENT_HOME/...`, not `$HOME/agents/$AGENT_NAME/...`.

## asdaaas.agent_dir

1. Explicit `env` → `env.agent_asdaaas_dir`
2. Else if `AGENTS_HOME_DIR` ≠ `config.agents_home` (test override) → flat under override
3. Else → `config.agent_asdaaas_dir` (nested OK)

## Why

Nested homes (e.g. `~/agents/LeviSmith/Squiggy`) broke any code that rebuilt
paths as `~/agents/$NAME`. That created ghost trees and split interjection /
compaction / adapter state from the live agent.
