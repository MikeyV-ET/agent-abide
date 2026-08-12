"""Injectable environment for TUI paths — testable without global Config."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import os


@dataclass
class TuiEnv:
    agents_home: Path
    sessions_root: Optional[Path] = None
    operator: str = ""
    api_url: Optional[str] = None

    def agent_home(self, name: str) -> Path:
        return self.agents_home / name

    def asdaaas_dir(self, name: str) -> Path:
        return self.agent_home(name) / "asdaaas"

    def health_file(self, name: str) -> Path:
        return self.asdaaas_dir(name) / "health.json"

    def gaze_file(self, name: str) -> Path:
        return self.asdaaas_dir(name) / "gaze.json"

    def commands_dir(self, name: str) -> Path:
        return self.asdaaas_dir(name) / "commands"

    def tui_inbox(self, name: str) -> Path:
        return self.asdaaas_dir(name) / "adapters" / "tui" / "inbox"

    @classmethod
    def from_defaults(cls, agents_home: Optional[str] = None) -> "TuiEnv":
        home = Path(agents_home or os.environ.get("AGENTS_HOME") or Path.home() / "agents")
        sessions = Path.home() / ".grok" / "sessions"
        return cls(agents_home=home, sessions_root=sessions)
