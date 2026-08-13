"""agent_dir must honor agents.json home (nested agent homes)."""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))


def test_env_agent_home_uses_config_home(tmp_path, monkeypatch):
    agents_home = tmp_path / "agents"
    nested = tmp_path / "LeviSmith" / "Lenny"
    nested.mkdir(parents=True)
    (nested / "asdaaas").mkdir()
    (agents_home / "Lenny").mkdir(parents=True)  # wrong flat path also exists

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    agents_json = {
        "settings": {"agents_dir": str(agents_home), "asdaaas_dir": str(tmp_path / "asdaaas")},
        "agents": {
            "Lenny": {"home": str(nested), "model": "grok-4.6"},
            "Sr": {},
        },
    }
    (cfg_dir / "agents.json").write_text(json.dumps(agents_json))
    monkeypatch.setenv("ASDAAAS_CONFIG", str(cfg_dir))

    # Reload config singleton — asdaaas_config caches at import
    import importlib
    import asdaaas_config
    importlib.reload(asdaaas_config)
    import asdaaas_env
    importlib.reload(asdaaas_env)
    import asdaaas
    importlib.reload(asdaaas)

    env = asdaaas_env.AsdaaasEnv.from_config()
    assert env.agent_home("Lenny") == nested
    assert env.agent_asdaaas_dir("Lenny") == nested / "asdaaas"
    assert asdaaas.agent_dir("Lenny") == nested / "asdaaas"
    # Sr still flat
    assert env.agent_home("Sr") == agents_home / "Sr"
