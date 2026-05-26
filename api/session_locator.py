"""
session_locator.py — Find session files for each agent.

Given agents.json config, resolves:
    - Grok agents: ~/.grok/sessions/<url-encoded-cwd>/<session-id>/updates.jsonl
    - Claude agents: ~/.claude/projects/<dash-encoded-cwd>/<session-id>.jsonl

Also provides agent listing with metadata.
"""

import json
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote


def _load_agents_config(config_path: Optional[Path] = None) -> dict:
    """Load agents.json. Checks ASDAAAS_CONFIG env, then default locations."""
    if config_path and config_path.is_file():
        with open(config_path) as f:
            return json.load(f)

    env = os.environ.get("ASDAAAS_CONFIG")
    if env and os.path.isfile(env):
        with open(env) as f:
            return json.load(f)

    # Default: agents.json next to this file's parent
    default = Path(__file__).parent.parent / "agents.json"
    if default.is_file():
        with open(default) as f:
            return json.load(f)

    raise FileNotFoundError("Cannot find agents.json")


class SessionLocator:
    """Resolve session file paths for agents."""

    def __init__(self, config_path: Optional[Path] = None):
        self._config = _load_agents_config(config_path)
        self._agents = self._config.get("agents", {})

    def list_agents(self) -> list[dict]:
        """Return list of agents with name, backend, and session file status."""
        result = []
        for name, cfg in self._agents.items():
            backend = cfg.get("backend", "grok")
            session_path = self.session_file(name)
            result.append({
                "name": name,
                "backend": backend,
                "home": cfg.get("home", ""),
                "session_file": str(session_path) if session_path else None,
                "has_session": session_path is not None and session_path.exists(),
            })
        return result

    def agent_config(self, name: str) -> Optional[dict]:
        """Get raw config for an agent."""
        return self._agents.get(name)

    def agent_backend(self, name: str) -> str:
        """Return 'grok' or 'claude' for the named agent."""
        cfg = self._agents.get(name, {})
        return cfg.get("backend", "grok")

    def session_file(self, name: str) -> Optional[Path]:
        """Resolve the session JSONL file path for an agent."""
        cfg = self._agents.get(name)
        if cfg is None:
            return None

        backend = cfg.get("backend", "grok")
        session_id = cfg.get("session", "")
        home = cfg.get("home", "")

        if not session_id:
            # Try health.json — asdaaas writes session_id there at runtime
            health_path = Path(home) / "asdaaas" / "health.json"
            if health_path.exists():
                try:
                    with open(health_path) as f:
                        health = json.load(f)
                    session_id = health.get("session_id", "")
                except (json.JSONDecodeError, OSError):
                    pass
            if not session_id:
                return None

        if backend == "grok":
            return self._grok_session_path(home, session_id)
        elif backend == "claude":
            return self._claude_session_path(home, session_id)
        return None

    def _grok_session_path(self, home: str, session_id: str) -> Path:
        """Grok: ~/.grok/sessions/<url-encoded-home>/<session-id>/updates.jsonl"""
        encoded = quote(home, safe="")
        return Path.home() / ".grok" / "sessions" / encoded / session_id / "updates.jsonl"

    def _claude_session_path(self, home: str, session_id: str) -> Path:
        """Claude: ~/.claude/projects/<dash-encoded-home>/<session-id>.jsonl"""
        encoded = home.replace("/", "-")
        if encoded.startswith("-"):
            pass  # Claude uses leading dash
        return Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"