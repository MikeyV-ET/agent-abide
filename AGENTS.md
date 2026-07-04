# agent-abide — Project Instructions

## Testing Contract

Tests named `test_e2e_*` must drive `main()` with MockBinary via the `asdaaas_env` fixture. Input enters only through adapter inboxes, doorbells, and commands. Assertions read only outboxes, health, doorbell/command state, and conversation.jsonl. They must not import private functions from asdaaas.

Every new engine feature ships with at least one such scenario.

Integration tests (direct function calls) are welcome but must not carry the `test_e2e_` prefix. Name them `test_integration_*` or `test_unit_*` as appropriate.

The enforcement meta-test (`test_e2e_convention.py`) verifies this contract by checking imports in all `test_e2e_*` files.

## Test Hierarchy

| Prefix | What it tests | Can import from asdaaas? | Drives main()? |
|--------|--------------|--------------------------|-----------------|
| `test_e2e_` | Full engine behavior | No | Yes (via asdaaas_env fixture) |
| `test_integration_` | Component interactions | Yes | No |
| `test_unit_` | Individual functions | Yes | No |
| `test_mock_` | MockBinary scenarios | Only MockBinary | Varies |

## Running Tests

```bash
# All tests
python3 -m pytest tests/ -v

# E2E only
python3 -m pytest tests/test_e2e_*.py -v

# Specific file
python3 -m pytest tests/test_integration_agent.py -v
```

## Code Style

- Use `|| true` when running tests in agent sessions to avoid doom loop detection.
- Filesystem-as-state: all agent communication goes through the file interface.
- Crash safety: use mkstemp+rename for atomic writes, never open(f, "w") on live state files.
