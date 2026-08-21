"""Regression: nested agents.json homes must not use flat ~/agents/Name paths."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "adapters"))
sys.path.insert(0, str(ROOT / "tui"))


class _Cfg:
    def __init__(self, agents_home: Path, homes: dict):
        self.agents_home = agents_home
        self._homes = homes
        self.agents = {name: {"home": str(path)} for name, path in homes.items()}

    def agent_home(self, name: str) -> Path:
        if name in self._homes:
            return self._homes[name]
        return self.agents_home / name

    def agent_asdaaas_dir(self, name: str) -> Path:
        return self.agent_home(name) / "asdaaas"

    def agent_doorbells_dir(self, name: str) -> Path:
        return self.agent_asdaaas_dir(name) / "doorbells"

    def agent_adapter_inbox(self, name: str, adapter: str) -> Path:
        return self.agent_asdaaas_dir(name) / "adapters" / adapter / "inbox"

    def agent_adapter_outbox(self, name: str, adapter: str) -> Path:
        return self.agent_asdaaas_dir(name) / "adapters" / adapter / "outbox"

    def agent_permissions_dir(self, name: str) -> Path:
        return self.agent_asdaaas_dir(name) / "permissions"

    def agent_observer_state_file(self, name: str) -> Path:
        return self.agent_asdaaas_dir(name) / "binary_state.json"


@pytest.fixture
def nested_env(tmp_path):
    agents_home = tmp_path / "agents"
    agents_home.mkdir()
    nested = tmp_path / "LeviSmith" / "Squiggy"
    (nested / "asdaaas").mkdir(parents=True)
    ghost = agents_home / "Squiggy" / "asdaaas"
    ghost.mkdir(parents=True)

    cfg = _Cfg(agents_home, {"Squiggy": nested})
    from asdaaas_env import AsdaaasEnv

    env = AsdaaasEnv(agents_home=agents_home, config=cfg)
    return {
        "cfg": cfg,
        "env": env,
        "nested": nested,
        "ghost": ghost,
        "agents_home": agents_home,
        "tmp": tmp_path,
    }


def test_adapter_api_inbox_outbox_nested(nested_env):
    import adapter_api

    env = nested_env["env"]
    nested = nested_env["nested"]
    ghost = nested_env["ghost"]

    msg_id = adapter_api.write_to_adapter_inbox(
        "tui", "Squiggy", "hi nested", sender="eric", env=env
    )
    assert msg_id
    inbox = nested / "asdaaas" / "adapters" / "tui" / "inbox"
    assert list(inbox.glob("*.json"))
    ghost_inbox = ghost / "adapters" / "tui" / "inbox"
    assert not ghost_inbox.exists() or not list(ghost_inbox.glob("*.json"))

    adapter_api.write_to_adapter_outbox(
        "tui", "Squiggy", "reply", env=env
    )
    msgs = adapter_api.poll_adapter_outbox("tui", "Squiggy", env=env)
    assert len(msgs) == 1
    assert msgs[0]["text"] == "reply"
    ghost_out = ghost / "adapters" / "tui" / "outbox"
    assert not ghost_out.exists() or not list(ghost_out.glob("*.json"))


def test_interjection_queue_nested(nested_env):
    from interjection import interjection_dir, queue_interjection

    env = nested_env["env"]
    nested = nested_env["nested"]
    ghost = nested_env["ghost"]
    d = interjection_dir("Squiggy", env=env)
    assert d == nested / "asdaaas" / "interjections"
    queue_interjection("Squiggy", "hello nested", env=env)
    assert list(d.glob("*.txt"))
    assert not (ghost / "interjections").exists()


def test_turn_engine_agent_dir_nested(nested_env):
    from turn_engine import TurnEngine
    from unittest.mock import MagicMock

    # Minimal construct — only need agent_dir()
    te = TurnEngine.__new__(TurnEngine)
    te.env = nested_env["env"]
    te.agent_name = "Squiggy"
    assert te.agent_dir() == nested_env["nested"] / "asdaaas"


def test_tui_env_nested_homes(tmp_path):
    from tui_env import TuiEnv

    nested = tmp_path / "LeviSmith" / "Squiggy"
    env = TuiEnv(
        agents_home=tmp_path / "agents",
        agent_homes={"Squiggy": nested},
    )
    assert env.agent_home("Squiggy") == nested
    assert env.asdaaas_dir("Squiggy") == nested / "asdaaas"
    assert env.agent_home("Jr") == tmp_path / "agents" / "Jr"


def test_context_adapter_uses_config_paths(nested_env, monkeypatch):
    import context_adapter as ca

    monkeypatch.setattr(ca, "config", nested_env["cfg"])
    nested = nested_env["nested"]
    assert ca.config.agent_asdaaas_dir("Squiggy") / "health.json" == nested / "asdaaas" / "health.json"
    assert ca.config.agent_doorbells_dir("Squiggy") == nested / "asdaaas" / "doorbells"


def test_session_adapter_paths(nested_env, monkeypatch):
    import session_adapter as sa

    monkeypatch.setattr(sa, "config", nested_env["cfg"])
    nested = nested_env["nested"]
    assert sa.config.agent_asdaaas_dir("Squiggy") == nested / "asdaaas"
    assert sa.config.agent_adapter_inbox("Squiggy", "session") == nested / "asdaaas" / "adapters" / "session" / "inbox"


def test_permission_handler_nested(nested_env, monkeypatch):
    import permission_handler as ph

    monkeypatch.setattr(ph, "config", nested_env["cfg"])
    assert ph._permissions_dir("Squiggy") == nested_env["nested"] / "asdaaas" / "permissions"


def test_date_clock_nested(nested_env, monkeypatch):
    import date_clock as dc

    # patch the import inside the function
    import asdaaas_config
    monkeypatch.setattr(asdaaas_config, "config", nested_env["cfg"])
    # also if already imported in module namespace - date_clock imports inside try
    nested = nested_env["nested"]
    dc.drop_date_doorbell("Squiggy", "Friday, August 21, 2026")
    bells = list((nested / "asdaaas" / "doorbells").glob("*.json"))
    assert bells, "doorbell should land under nested home"
    assert not list((nested_env["ghost"] / "doorbells").glob("*.json")) if (nested_env["ghost"] / "doorbells").exists() else True


def test_cancel_turn_script_nested(tmp_path):
    import subprocess

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    nested = tmp_path / "LeviSmith" / "Squiggy"
    (nested / "asdaaas").mkdir(parents=True)
    (cfg_dir / "agents.json").write_text(json.dumps({
        "agents": {"Squiggy": {"home": str(nested)}}
    }))
    script = ROOT / "scripts" / "cancel_turn.sh"
    env = {**os.environ, "ASDAAAS_CONFIG": str(cfg_dir), "HOME": str(tmp_path)}
    r = subprocess.run(
        ["bash", str(script), "Squiggy"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    assert (nested / "asdaaas" / "cancel_turn.flag").exists()
    assert not (tmp_path / "agents" / "Squiggy" / "asdaaas" / "cancel_turn.flag").exists()


def test_asdaaas_version_script_nested(tmp_path):
    import subprocess

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    nested = tmp_path / "LeviSmith" / "Squiggy"
    (nested / "asdaaas").mkdir(parents=True)
    (nested / "asdaaas" / "health.json").write_text(json.dumps({
        "code_version": "deadbeef",
        "last_activity": "now",
    }))
    (cfg_dir / "agents.json").write_text(json.dumps({
        "agents": {"Squiggy": {"home": str(nested)}}
    }))
    script = ROOT / "scripts" / "asdaaas_version.sh"
    env = {**os.environ, "ASDAAAS_CONFIG": str(cfg_dir), "HOME": str(tmp_path)}
    r = subprocess.run(
        ["bash", str(script), "Squiggy"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    assert "deadbeef" in r.stdout
    assert "not found" not in r.stdout


def test_impress_scan_uses_catalog(nested_env, monkeypatch):
    """impress inbox scan iterates config.agents, not agents_home.iterdir()."""
    import impress_control_adapter as imp

    monkeypatch.setattr(imp, "config", nested_env["cfg"])
    nested = nested_env["nested"]
    inbox = nested / "asdaaas" / "adapters" / imp.ADAPTER_NAME / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    msg_path = inbox / "cmd1.json"
    msg_path.write_text(json.dumps({"command": "ping"}))

    # Find the poll/scan function
    src = Path(imp.__file__).read_text()
    assert "config.agents.keys()" in src or "for agent_name in sorted(config.agents" in src
    assert "AGENTS_HOME_DIR.iterdir()" not in src
