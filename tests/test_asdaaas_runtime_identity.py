"""asdaaas_runtime shared across __main__-style and import asdaaas."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))


def test_runtime_survives_second_asdaaas_import(tmp_path, monkeypatch):
    import asdaaas_runtime as rt
    rt.set_identity(model_id="opus", session_id="sess-1", backend_type="claude", code_version_val="abc")

    # Simulate second load path: import asdaaas module (may re-exec top-level)
    import asdaaas
    importlib.reload(asdaaas)

    # Identity must still be claude/sess — not wiped to grok defaults
    assert rt.current_backend_type == "claude"
    assert rt.current_session_id == "sess-1"
    assert rt.current_model_id == "opus"

    # write_health must read runtime
    monkeypatch.setenv("HOME", str(tmp_path))
    # agent_dir uses home/agents/...
    (tmp_path / "agents" / "Astro" / "asdaaas").mkdir(parents=True)
    # asdaaas.agent_dir may use different layout — call write_health with env mock if needed
    from asdaaas_env import AsdaaasEnv
    # minimal: just check write_health uses _rt fields in constructed dict by patching agent_dir
    written = {}

    def fake_agent_dir(name, env=None):
        d = tmp_path / "agents" / name / "asdaaas"
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr(asdaaas, "agent_dir", fake_agent_dir)
    asdaaas.write_health("Astro", "working", "test", total_tokens=100, context_window=1000000)
    health = json.loads((tmp_path / "agents" / "Astro" / "asdaaas" / "health.json").read_text())
    assert health["backend"] == "claude"
    assert health["session_id"] == "sess-1"
    assert health["model"] == "opus"
