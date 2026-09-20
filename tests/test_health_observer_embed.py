"""write_health embeds last binary_state.json even after TTL."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

import asdaaas
import asdaaas_runtime as rt
from binary_state.machine import BinaryActivityMachine


def test_write_health_embeds_stale_observer(tmp_path, monkeypatch):
    rt.set_identity(model_id="m", session_id="s", backend_type="claude", reasoning_effort="medium")
    agent = "Astro"
    base = tmp_path / "agents" / agent / "asdaaas"
    base.mkdir(parents=True)
    obs = {
        "state": "BUSY",
        "since": 1.0,
        "written_at": time.time() - 10,
        "expires_at": time.time() - 5,  # expired
        "last_event_type": "claude:text",
        "model_id": "claude-opus-5",
        "reasoning_effort": "medium",
        "doom_loop": False,
        "turn_event_count": 3,
        "pid": 99,
    }
    (base / "binary_state.json").write_text(json.dumps(obs))

    def fake_agent_dir(name, env=None):
        return base

    monkeypatch.setattr(asdaaas, "agent_dir", fake_agent_dir)
    asdaaas.write_health(agent, "active", "test", total_tokens=100, context_window=1000000)
    health = json.loads((base / "health.json").read_text())
    assert health["backend"] == "claude"
    assert health["reasoning_effort"] == "medium"
    assert health["observer"]["state"] == "BUSY"
    assert health["observer"]["last_event_type"] == "claude:text"
    assert health["observer"]["model_id"] == "claude-opus-5"


def test_read_state_file_ttl():
    import tempfile, os
    d = tempfile.mkdtemp()
    path = os.path.join(d, "s.json")
    with open(path, "w") as f:
        json.dump({"state": "IDLE", "expires_at": time.time() - 1}, f)
    assert BinaryActivityMachine.read_state_file(path) is None
    assert BinaryActivityMachine.read_state_file(path, ignore_ttl=True)["state"] == "IDLE"
