"""compaction_hook writes binary_state under AGENT_HOME / cwd, not flat ~/agents/Name."""
import json
import os
import subprocess
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "compaction_hook.sh"


def _run(envelope: dict, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(HOOK)],
        input=json.dumps(envelope),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_compaction_uses_agent_home_nested(tmp_path):
    nested = tmp_path / "LeviSmith" / "Squiggy"
    (nested / "asdaaas").mkdir(parents=True)
    # ghost flat
    ghost = tmp_path / "agents" / "Squiggy" / "asdaaas"
    ghost.mkdir(parents=True)

    env = {**os.environ, "HOME": str(tmp_path), "AGENT_HOME": str(nested)}
    env.pop("AGENT_NAME", None)
    envelope = {
        "hookEventName": "post_compact",
        "sessionId": "sess-nested",
        "cwd": str(nested),
        "source": "auto",
    }
    r = _run(envelope, env)
    assert r.returncode == 0, r.stderr
    state_path = nested / "asdaaas" / "binary_state.json"
    assert state_path.exists()
    data = json.loads(state_path.read_text())
    assert data["compaction"]["phase"] == "complete"
    assert data["compaction"]["session_id"] == "sess-nested"
    assert not (ghost / "binary_state.json").exists()


def test_compaction_falls_back_to_cwd(tmp_path):
    nested = tmp_path / "LeviSmith" / "Squiggy"
    (nested / "asdaaas").mkdir(parents=True)
    env = {**os.environ, "HOME": str(tmp_path)}
    env.pop("AGENT_HOME", None)
    envelope = {
        "hookEventName": "pre_compact",
        "sessionId": "sess-cwd",
        "cwd": str(nested),
        "source": "manual",
    }
    r = _run(envelope, env)
    assert r.returncode == 0, r.stderr
    data = json.loads((nested / "asdaaas" / "binary_state.json").read_text())
    assert data["compaction"]["phase"] == "in_flight"
