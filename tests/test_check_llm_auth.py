import json
import subprocess
import sys
from pathlib import Path

CLI = Path(__file__).resolve().parents[1] / "scripts" / "check_llm_auth.py"


def run(*args, home=None):
    cmd = [sys.executable, str(CLI), *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def test_empty_auth_needed(tmp_path):
    grok = tmp_path / ".grok"
    grok.mkdir()
    (grok / "auth.json").write_text("")
    r = run("--home", str(tmp_path), "--agent", "Wend")
    assert r.returncode == 2
    payload = json.loads(r.stdout)
    assert payload["authNeeded"] is True
    assert "Wend" in payload["agents"]
    assert "AUTH_NEEDED" in r.stderr
    assert "password" not in r.stdout.lower()


def test_usable_auth_ok(tmp_path):
    grok = tmp_path / ".grok"
    grok.mkdir()
    (grok / "auth.json").write_text(json.dumps({"access_token": "x" * 40}) + "\n")
    r = run("--home", str(tmp_path), "--agent", "Wend")
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    assert payload["authNeeded"] is False
    assert payload["ok"] is True


def test_missing_auth_needed(tmp_path):
    r = run("--home", str(tmp_path), "--agent", "Wend")
    assert r.returncode == 2
    assert json.loads(r.stdout)["rows"][0]["reason"] == "missing"


def test_non_json_blob_still_usable(tmp_path):
    grok = tmp_path / ".grok"
    grok.mkdir()
    (grok / "auth.json").write_bytes(b"x" * 80)
    r = run("--home", str(tmp_path), "--agent", "Wend")
    assert r.returncode == 0
    assert json.loads(r.stdout)["ok"] is True


def test_holder_fallback_uses_operator_home_when_copy_expired(tmp_path):
    agent = tmp_path / "agent"
    (agent / ".grok").mkdir(parents=True)
    blob = {
        "https://auth.x.ai::test": {
            "expires_at": "2020-01-01T00:00:00Z",
            "key": "x" * 20,
        }
    }
    (agent / ".grok" / "auth.json").write_text(json.dumps(blob))
    holder = tmp_path / "holder"
    (holder / ".grok").mkdir(parents=True)
    (holder / ".grok" / "auth.json").write_text(json.dumps({"access_token": "x" * 40}))
    r = run(
        "--home",
        str(agent),
        "--agent",
        "Squiggy",
        "--holder-fallback",
        "--holder-home",
        str(holder),
    )
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["rows"][0]["source"] == "holder"


def test_expired_auth_needed(tmp_path):
    grok = tmp_path / ".grok"
    grok.mkdir()
    blob = {
        "https://auth.x.ai::test": {
            "expires_at": "2020-01-01T00:00:00Z",
            "key": "x" * 20,
        }
    }
    (grok / "auth.json").write_text(json.dumps(blob))
    r = run("--home", str(tmp_path), "--agent", "Wend")
    assert r.returncode == 2
    payload = json.loads(r.stdout)
    assert payload["authNeeded"] is True
    assert payload["rows"][0]["reason"] == "expired"
