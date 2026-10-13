"""start_agent writes history/config and never mints a session id."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from start_agent import start_agent, write_history_config, ensure_home


def test_history_config_on_ensure(tmp_path):
    home = ensure_home("LeviSmith", "Demo", agents_dir=tmp_path)
    cfg = home / "asdaaas" / "history" / "config.json"
    assert cfg.is_file()
    data = json.loads(cfg.read_text())
    assert data.get("tail_grok") is True
    assert data.get("tail_stream") is True
    assert data.get("owner") == "backend"


def test_start_without_session_does_not_mint(tmp_path):
    roster = tmp_path / "agents.json"
    roster.write_text(json.dumps({"settings": {"agents_dir": str(tmp_path)}, "agents": {}}) + "\n")
    result = start_agent(
        "LeviSmith",
        "Demo",
        agents_json=roster,
        launch=False,
    )
    assert result["session"] is None
    cfg = json.loads(roster.read_text())
    row = cfg["agents"]["Demo"]
    assert "session" not in row
    assert row["family"] == "LeviSmith"
    assert (Path(result["home"]) / "asdaaas" / "history" / "config.json").is_file()


def test_start_attaches_existing_uuid(tmp_path, monkeypatch):
    roster = tmp_path / "agents.json"
    roster.write_text(json.dumps({"settings": {"agents_dir": str(tmp_path / "agents")}, "agents": {}}) + "\n")
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    sid = "01a1366b-0432-7220-8079-8311f4c98e81"
    grok = tmp_path / "grok" / "sessions"
    old = grok / ("%2Ftmp%2Fold") / sid
    old.mkdir(parents=True)
    (old / "updates.jsonl").write_text("{}\n")
    (old / "summary.json").write_text(json.dumps({"info": {"id": sid, "cwd": "/tmp/old"}}) + "\n")
    result = start_agent(
        "LeviSmith",
        "Holtish",
        session=sid,
        agents_json=roster,
        launch=False,
        grok_sessions=grok,
    )
    assert result["session"] == sid
    row = json.loads(roster.read_text())["agents"]["Holtish"]
    assert row["session"] == sid
    dest = Path(result["home"]).resolve()
    encoded = str(dest).replace("/", "%2F")
    moved = grok / encoded / sid
    assert moved.is_dir()
    assert not old.exists()
    assert json.loads((moved / "summary.json").read_text())["info"]["cwd"] == str(dest)


def test_parse_start_tokens_model_and_uuid():
    from start_agent import parse_start_tokens
    a = parse_start_tokens("testagent scratch grok-4.7-build-fast")
    assert a["family"] == "testagent" and a["name"] == "scratch"
    assert a["session"] is None and a["model"] == "grok-4.7-build-fast"
    b = parse_start_tokens("LeviSmith Holt 01a1366b-0432-7220-8079-8311f4c98e81 grok-4.6")
    assert b["session"].startswith("01a1366b") and b["model"] == "grok-4.6"


def test_testagent_may_launch_without_session(tmp_path):
    from start_agent import MINT_OK_FAMILIES, start_agent
    assert "testagent" in MINT_OK_FAMILIES
    roster = tmp_path / "agents.json"
    roster.write_text(json.dumps({"settings": {"agents_dir": str(tmp_path)}, "agents": {}}) + "\n")
    r = start_agent(
        "testagent",
        "scratch",
        agents_json=roster,
        model="grok-4.7-build-fast",
        launch=False,
    )
    row = json.loads(roster.read_text())["agents"]["scratch"]
    assert "session" not in row
    assert row["model"] == "grok-4.7-build-fast"
    assert row["family"] == "testagent"
