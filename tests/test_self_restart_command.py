"""Self-restart command helper finds restart_agent.sh."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

import asdaaas

AGENT = "TestOccupancyXyz"


def _cleanup_pid():
    try:
        asdaaas.self_restart_pid_path(AGENT).unlink()
    except OSError:
        pass


def test_schedule_self_restart_finds_script():
    script = Path(asdaaas.__file__).resolve().parent.parent / "scripts" / "restart_agent.sh"
    assert script.is_file()


def test_schedule_self_restart_spawns(monkeypatch):
    spawned = {}

    def fake_popen(cmd, **kwargs):
        spawned["cmd"] = cmd
        spawned["kwargs"] = kwargs

        class P:
            pid = 4242

        return P()

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    _cleanup_pid()
    ok = asdaaas.schedule_self_restart(AGENT, reason="test", delay_s=0.5)
    assert ok is True
    assert "restart_agent.sh" in spawned["cmd"][2]
    assert AGENT in spawned["cmd"][2]
    assert spawned["kwargs"].get("start_new_session") is True
    pid_path = asdaaas.self_restart_pid_path(AGENT)
    assert pid_path.read_text().strip() == "4242"
    _cleanup_pid()


def test_schedule_self_restart_cancels_prior(monkeypatch):
    class P:
        def __init__(self, pid):
            self.pid = pid

    n = [100]
    pids = []

    def fake_popen(cmd, **kwargs):
        n[0] += 1
        p = P(n[0])
        pids.append(p.pid)
        return p

    killed = []

    def fake_killpg(pid, sig):
        killed.append(("killpg", pid, sig))

    def fake_kill(pid, sig):
        killed.append(("kill", pid, sig))

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(os, "killpg", fake_killpg)
    monkeypatch.setattr(os, "kill", fake_kill)
    _cleanup_pid()
    assert asdaaas.schedule_self_restart(AGENT, reason="a", delay_s=9)
    assert asdaaas.schedule_self_restart(AGENT, reason="b", delay_s=9)
    assert pids == [101, 102]
    assert any(k[1] == 101 for k in killed)
    assert asdaaas.self_restart_pid_path(AGENT).read_text().strip() == "102"
    _cleanup_pid()


def test_cancel_scheduled_self_restart_missing_is_false():
    _cleanup_pid()
    assert asdaaas.cancel_scheduled_self_restart(AGENT) is False


def test_restart_agent_sh_holds_per_agent_lock():
    src = (Path(asdaaas.__file__).resolve().parent.parent / "scripts" / "restart_agent.sh").read_text()
    assert "flock -n 8" in src
    assert 'restart_agent_${agent}.lock' in src
