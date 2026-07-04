"""
asdaaas_env.py — Composition root for ASDAAAS.
================================================
All resolved paths and settings for one asdaaas instance.
Passed explicitly to functions instead of using module-level globals.

Usage (production):
    env = AsdaaasEnv.from_config()
    
Usage (testing):
    env = AsdaaasEnv(agents_home=tmp_path / "agents", ...)
"""

from pathlib import Path
from typing import Optional


class AsdaaasEnv:
    """All resolved paths and settings for one asdaaas instance."""

    def __init__(self, agents_home: Path, asdaaas_dir: Optional[Path] = None,
                 config=None):
        self.agents_home = Path(agents_home)
        self.asdaaas_dir = Path(asdaaas_dir) if asdaaas_dir else self.agents_home
        self.config = config

    def agent_home(self, name: str) -> Path:
        return self.agents_home / name

    def agent_asdaaas_dir(self, name: str) -> Path:
        return self.agents_home / name / "asdaaas"

    def adapter_dir(self, name: str, adapter: str) -> Path:
        return self.agent_asdaaas_dir(name) / "adapters" / adapter

    def adapter_inbox(self, name: str, adapter: str) -> Path:
        return self.adapter_dir(name, adapter) / "inbox"

    def adapter_outbox(self, name: str, adapter: str) -> Path:
        return self.adapter_dir(name, adapter) / "outbox"

    def doorbells_dir(self, name: str) -> Path:
        return self.agent_asdaaas_dir(name) / "doorbells"

    def commands_dir(self, name: str) -> Path:
        return self.agent_asdaaas_dir(name) / "commands"

    def localmail_inbox(self, name: str) -> Path:
        return self.adapter_dir(name, "localmail") / "inbox"

    def localmail_payloads(self, name: str) -> Path:
        return self.adapter_dir(name, "localmail") / "payloads"

    @classmethod
    def from_config(cls) -> 'AsdaaasEnv':
        """Build from current config singleton (backward compat)."""
        from asdaaas_config import config
        return cls(
            agents_home=config.agents_home,
            asdaaas_dir=config.asdaaas_dir,
            config=config,
        )
