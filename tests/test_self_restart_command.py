"""Self-restart command helper finds restart_agent.sh."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

import asdaaas


def test_schedule_self_restart_finds_script():
    script = Path(asdaaas.__file__).resolve().parent.parent / "scripts" / "restart_agent.sh"
    assert script.is_file()


def test_schedule_self_restart_spawns(monkeypatch):
    spawned = {}

    def fake_popen(cmd, **kwargs):
        spawned["cmd"] = cmd
        spawned["kwargs"] = kwargs

        class P:
            pid = 1

        return P()

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    ok = asdaaas.schedule_self_restart("Trip-G", reason="test", delay_s=0.5)
    assert ok is True
    assert "restart_agent.sh" in spawned["cmd"][2]
    assert "Trip-G" in spawned["cmd"][2]
    assert spawned["kwargs"].get("start_new_session") is True
