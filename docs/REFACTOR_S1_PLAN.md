# S1 Refactor: Composition Root (AsdaaasEnv)

**Author:** Sr
**Date:** 2026-07-04
**Status:** In Progress
**Branch:** refactor/s1-composition-root
**Repo:** ~/projects/agent-abide-dev/

## Goal
Replace module-level globals with an injectable AsdaaasEnv object so paths/config can be overridden for testing without monkeypatching.

## Current globals to replace

| File | Global | Line | What it does |
|------|--------|------|-------------|
| asdaaas.py | `ASDAAAS_DIR = config.asdaaas_dir` | 55 | Base asdaaas dir |
| asdaaas.py | `AGENTS_HOME_DIR = config.agents_home` | 57 | Agents home dir |
| localmail.py | `AGENTS_HOME_DIR = config.agents_home` | 65 | Agents home dir |
| adapter_api.py | `AGENTS_HOME_DIR = config.agents_home` | 51 | Agents home dir |
| asdaaas_config.py | `config = AsdaaasConfig()` | module-level | Singleton instance |

## AsdaaasEnv class design

```python
class AsdaaasEnv:
    """All resolved paths and settings for one asdaaas instance."""
    def __init__(self, agents_home: Path, asdaaas_dir: Path, config: AsdaaasConfig):
        self.agents_home = agents_home
        self.asdaaas_dir = asdaaas_dir
        self.config = config
    
    def agent_home(self, name: str) -> Path:
        return self.agents_home / name
    
    def agent_asdaaas_dir(self, name: str) -> Path:
        return self.agents_home / name / "asdaaas"
    
    def adapter_dir(self, name: str, adapter: str) -> Path:
        return self.agent_asdaaas_dir(name) / "adapters" / adapter
    
    @classmethod
    def from_config(cls) -> 'AsdaaasEnv':
        """Build from current config singleton (backward compat)."""
        from asdaaas_config import config
        return cls(config.agents_home, config.asdaaas_dir, config)
```

## Conversion order (simplest → most complex)
1. ✅ Create AsdaaasEnv class in new file `core/asdaaas_env.py`
2. localmail.py — simplest consumer, good test coverage
3. interjection.py — small file (106 lines)
4. adapter_api.py — medium (1125 lines)
5. asdaaas.py — main, biggest (3309 lines), last

## Conversion pattern per module
- Add `env: Optional[AsdaaasEnv] = None` parameter to public functions
- Inside function: `env = env or AsdaaasEnv.from_config()`
- Replace `AGENTS_HOME_DIR` references with `env.agents_home`
- Module-level global stays temporarily for backward compat
- Remove global only after all callers pass env

## Safety rules
- Run full test suite after each module conversion
- No behavior changes — only path source changes
- Commit per module
