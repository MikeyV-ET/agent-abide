# ASDAAAS Adapter Pattern

## Overview

Adapters connect ASDAAAS agents to external systems (IRC, Slack, TUI, etc.).
Each adapter is a standalone process that communicates with agents via the
filesystem-based message bus (`adapter_api.py`).

## Architecture

```
  [IRC]         [Slack]        [TUI]         [Your transport]
    |               |            |                 |
  irc_adapter   slack_adapter  tui_adapter    your_adapter
    |               |            |                 |
    +---------- adapter_api.py --------------------+
                    |                    |
    ~/agents/<Agent>/asdaaas/adapters/<name>/inbox/    (inbound)
    ~/agents/<Agent>/asdaaas/adapters/<name>/outbox/   (outbound)
                    |
               asdaaas.py (per-agent engine)
```

## Adapter Types

| Type | Role | Examples |
|------|------|---------|
| **Direct** | Two-way transport (mouth + ear) | IRC, Slack, TUI |
| **Notify** | Doorbell-only (pointer to data) | Localmail, Context |
| **Control** | Button + receipt (command/result) | Session, Impress, Meet |

## Message Flow

### Inbound (external -> agent)
1. Adapter receives from external transport
2. Adapter calls `adapter_api.write_to_adapter_inbox(adapter_name, to, text, sender, meta)`
3. ASDAAAS polls agent's adapter inboxes, delivers as prompt or doorbell
4. Agent responds

### Outbound (agent -> external)
1. ASDAAAS writes agent speech to gaze-targeted adapter outbox
2. Adapter polls `adapter_api.poll_adapter_outbox(adapter_name, agent_name)`
3. Adapter sends to external transport

## Complete Adapter Package Checklist

A "shippable" adapter includes all of the following:

### Required Files
```
adapters/
  your_adapter.py       # The adapter process
ops/
  launch_your_adapter.sh  # Launch script (setsid nohup, log rotation)
tests/
  test_your_adapter.py  # Unit tests
```

### Required in your_adapter.py
- [ ] Import `adapter_api` and `asdaaas_config`
- [ ] Call `adapter_api.ensure_dirs(ADAPTER_NAME)` at startup
- [ ] Call `adapter_api.register_adapter(name, capabilities, config)` at startup
- [ ] Write inbound messages via `adapter_api.write_to_adapter_inbox()`
- [ ] Poll outbound via `adapter_api.poll_adapter_outbox()`
- [ ] Handle `clean_response()` for output filtering (suppress `noted`, strip headers)
- [ ] Periodic heartbeat via `adapter_api.update_heartbeat(ADAPTER_NAME)`
- [ ] Graceful shutdown on SIGTERM
- [ ] `argparse` CLI with `--agents` flag for agent subset
- [ ] Timestamped logging via `tprint()` or `logging`

### Required Documentation
- [ ] Docstring at top of adapter file explaining what it connects to
- [ ] Entry in `ADAPTER_CATALOG.md` with status, capabilities, config
- [ ] Any external dependencies listed in `requirements.txt`
- [ ] Entry in `sync_to_public.sh` mappings (if syncing to public repo)

### Required Tests
- [ ] Message parsing (inbound format -> adapter_api calls)
- [ ] Response cleaning (suppress noted, strip headers, handle edge cases)
- [ ] CLI argument parsing
- [ ] At least one integration test with mock agent data

### If External Dependencies
- [ ] Listed in `requirements.txt` with version constraint
- [ ] Documented in adapter docstring
- [ ] Adapter fails gracefully with clear error if dependency missing

## Developer Shipping Checklist

When shipping any feature (adapter, fix, or enhancement):

1. **Code** -- implementation + tests passing
2. **requirements.txt** -- any new dependencies added with version constraints
3. **ADAPTER_CATALOG.md** -- updated if adapter status changed
4. **agents.json** -- updated if new adapter needs config entries
5. **ASDAAAS_AGENT_INSTRUCTIONS.md** -- updated if agents need new operational knowledge
6. **sync_to_public.sh** -- new files added to mappings
7. **Run sync** -- `bash sync_to_public.sh` to push to public repo
8. **Commit both repos** -- private (mikeyv-infra) and public (agentabide)
9. **Lab notebook** -- document what shipped, commit hashes, test count

## adapter_api.py Quick Reference

| Function | Purpose |
|----------|---------|
| `write_to_adapter_inbox(adapter, to, text, sender, meta)` | Write inbound message |
| `poll_adapter_outbox(adapter, agent)` | Read agent responses |
| `ensure_dirs(adapter)` | Create directories at startup |
| `register_adapter(name, capabilities, config)` | Register with ASDAAAS |
| `update_heartbeat(adapter)` | Signal adapter is alive |

## Writing a New Adapter

1. Copy `control_adapter_template.py` as starting point
2. Replace transport-specific code
3. Add to `requirements.txt` if external deps needed
4. Write tests
5. Create launch script
6. Add to `ADAPTER_CATALOG.md`
7. Add to `sync_to_public.sh` mappings
8. Run full test suite: `pytest tests/ -q`
