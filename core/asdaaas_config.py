"""
asdaaas_config.py — Central configuration for ASDAAAS.
======================================================
Single source of truth for all paths and settings. Other modules import
from here instead of hardcoding paths.

Config resolution order:
  1. ASDAAAS_CONFIG env var pointing to a JSON file
  2. config.json in the same directory as this file (install dir)
  3. Built-in defaults (~/asdaaas, ~/agents)

Usage:
  from asdaaas_config import config
  print(config.agents_home)    # Path object
  print(config.asdaaas_dir)    # Path object
  print(config.agent_home("Sr"))  # Path to agent's home dir
"""

import json
import os
from pathlib import Path
from typing import Optional


class AsdaaasConfig:
    """Immutable configuration loaded once at import time."""

    def __init__(self):
        self._data = self._load()
        self._agents_home = Path(self._data.get("agents_home",
            os.path.expanduser("~/agents")))
        self._asdaaas_dir = Path(self._data.get("asdaaas_dir",
            os.path.expanduser("~/asdaaas")))
        self._agents = self._data.get("agents", {})
        self._grok_sessions_dir = self._resolve_sessions_dir(
            self._data.get("grok_sessions_dir"))

    def _load(self):
        # 1. Env var — accepts a file or directory
        env_path = os.environ.get("ASDAAAS_CONFIG")
        if env_path:
            p = Path(env_path)
            if p.is_dir():
                for name in ("config.json", "agents.json"):
                    candidate = p / name
                    if candidate.is_file():
                        with open(candidate) as f:
                            return self._normalize(json.load(f))
            elif p.is_file():
                with open(p) as f:
                    return self._normalize(json.load(f))

        # 2. config.json next to this file
        here = Path(__file__).parent
        local_config = here / "config.json"
        if local_config.is_file():
            with open(local_config) as f:
                return self._normalize(json.load(f))

        # 3. agents.json next to this file (existing config format)
        agents_json = here / "agents.json"
        if agents_json.is_file():
            with open(agents_json) as f:
                return self._normalize(json.load(f))

        # 4. Search parent directories (for split layouts like core/ + adapters/)
        for parent in here.parents:
            for name in ("config.json", "agents.json"):
                candidate = parent / name
                if candidate.is_file():
                    with open(candidate) as f:
                        return self._normalize(json.load(f))
                # Also check core/ subdirectory
                candidate = parent / "core" / name
                if candidate.is_file():
                    with open(candidate) as f:
                        return self._normalize(json.load(f))
            # Stop at filesystem root or home
            if parent == parent.parent or parent == Path.home():
                break

        # 5. Defaults
        return {}

    def _normalize(self, data):
        """Normalize agents.json format to config format."""
        # agents.json uses settings.agents_dir; config.json uses agents_home
        if "settings" in data and "agents_home" not in data:
            settings = data["settings"]
            data["agents_home"] = settings.get("agents_dir",
                os.path.expanduser("~/agents"))
            data["asdaaas_dir"] = settings.get("asdaaas_dir",
                settings.get("asdaaas_system_dir",
                    os.path.expanduser("~/asdaaas")))
        return data

    def _resolve_sessions_dir(self, explicit):
        """Find grok sessions directory. Explicit config wins, then auto-detect."""
        if explicit:
            return Path(explicit)
        # Standard location
        standard = Path.home() / ".grok" / "sessions"
        if standard.is_dir():
            return standard
        # Multi-user grok install: ~/.grok-users/<identity>/.grok/sessions/
        grok_users = Path.home() / ".grok-users"
        if grok_users.is_dir():
            for user_dir in grok_users.iterdir():
                candidate = user_dir / ".grok" / "sessions"
                if candidate.is_dir():
                    return candidate
        # Fallback to standard (may not exist yet)
        return standard

    @property
    def grok_sessions_dir(self) -> Path:
        """Directory containing grok session data."""
        return self._grok_sessions_dir

    @property
    def agents_home(self) -> Path:
        """Parent directory containing all agent directories."""
        return self._agents_home

    @property
    def asdaaas_dir(self) -> Path:
        """Shared ASDAAAS system directory (running_agents, adapters)."""
        return self._asdaaas_dir

    @property
    def adapters_dir(self) -> Path:
        """Adapter registration directory."""
        return self._asdaaas_dir / "adapters"

    @property
    def running_agents_file(self) -> Path:
        return self._asdaaas_dir / "running_agents.json"

    @property
    def bugs_dir(self) -> Path:
        """Deprecated alias. Use issues_dir."""
        return self.issues_dir

    @property
    def issues_dir(self) -> Path:
        return self._agents_home / "issues"

    @property
    def agents(self) -> dict:
        """Per-agent config from config.json (session IDs, models, etc.)."""
        return self._agents

    def agent_home(self, agent_name: str) -> Path:
        """Home directory for a specific agent."""
        # Check if agent has a custom home in config
        agent_cfg = self._agents.get(agent_name, {})
        if "home" in agent_cfg:
            return Path(agent_cfg["home"])
        return self._agents_home / agent_name

    def agent_asdaaas_dir(self, agent_name: str) -> Path:
        """Per-agent asdaaas state directory."""
        return self.agent_home(agent_name) / "asdaaas"

    def agent_yolo(self, agent_name: str) -> bool:
        """Whether this agent runs in yolo mode (default True)."""
        return self._agents.get(agent_name, {}).get("yolo", True)

    def agent_mentor(self, agent_name: str) -> Optional[str]:
        """Mentor agent name for permission approval (None if no mentor)."""
        return self._agents.get(agent_name, {}).get("mentor")

    def agent_context_window(self, agent_name: str) -> Optional[int]:
        """Per-agent context window override (None = use default)."""
        return self._agents.get(agent_name, {}).get("context_window")

    def agent_allow_kinds(self, agent_name: str) -> list[str]:
        """Tool kinds pre-approved without mentor permission (default [])."""
        return self._agents.get(agent_name, {}).get("allow_kinds", [])

    def agent_sandbox(self, agent_name: str) -> Optional[str]:
        """Sandbox profile (workspace, read-only, strict, or custom). None = off."""
        return self._agents.get(agent_name, {}).get("sandbox")

    def agent_allow_rules(self, agent_name: str) -> list[str]:
        """Permission allow rules, e.g. ['Read', 'Edit(/path/**)']."""
        return self._agents.get(agent_name, {}).get("allow_rules", [])

    def agent_deny_rules(self, agent_name: str) -> list[str]:
        """Permission deny rules, e.g. ['Bash(rm*)']."""
        return self._agents.get(agent_name, {}).get("deny_rules", [])

    def agent_permission_mode(self, agent_name: str) -> Optional[str]:
        """Permission mode (default, acceptEdits, auto, bypassPermissions). None = binary default."""
        return self._agents.get(agent_name, {}).get("permission_mode")

    def agent_reasoning_effort(self, agent_name: str) -> Optional[str]:
        """Reasoning effort level: xhigh, high, medium, low. None = binary default."""
        return self._agents.get(agent_name, {}).get("reasoning_effort")

    def agent_backend(self, agent_name: str) -> str:
        """Backend type: 'grok' (default) or 'claude'."""
        return self._agents.get(agent_name, {}).get("backend", "grok")

    def agent_permissions_dir(self, agent_name: str) -> Path:
        """Directory for permission request/decision files."""
        return self.agent_asdaaas_dir(agent_name) / "permissions"

    def agent_observer_enabled(self, agent_name: str) -> bool:
        """Whether the binary state observer sidecar is enabled for this agent."""
        return self._agents.get(agent_name, {}).get("observer_enabled", False)

    def agent_interjection_enabled(self, agent_name: str) -> bool:
        """Whether mid-turn message interjection via BASH_ENV is enabled for this agent."""
        return self._agents.get(agent_name, {}).get("interjection_enabled", False)

    def agent_observer_state_file(self, agent_name: str) -> Path:
        """Path to the observer's state file for this agent."""
        return self.agent_asdaaas_dir(agent_name) / "binary_state.json"

    def agent_doorbells_dir(self, agent_name: str) -> Path:
        return self.agent_asdaaas_dir(agent_name) / "doorbells"

    def agent_adapter_inbox(self, agent_name: str, adapter_name: str) -> Path:
        return self.agent_asdaaas_dir(agent_name) / "adapters" / adapter_name / "inbox"

    def agent_adapter_outbox(self, agent_name: str, adapter_name: str) -> Path:
        return self.agent_asdaaas_dir(agent_name) / "adapters" / adapter_name / "outbox"

    # Legacy compat aliases
    @property
    def hub_dir(self) -> Path:
        return self._asdaaas_dir

    @property
    def inbox_dir(self) -> Path:
        return self._asdaaas_dir / "inbox"

    @property
    def outbox_dir(self) -> Path:
        return self._asdaaas_dir / "outbox"


# Singleton — loaded once at import time
config = AsdaaasConfig()
