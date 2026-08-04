import json
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))
from pathlib import Path
from status_read import telemetry_from_files, code_version_stale

def test_telemetry(tmp_path):
    h = tmp_path / "health.json"
    g = tmp_path / "gaze.json"
    h.write_text(json.dumps({
        "status": "working",
        "totalTokens": 1000,
        "contextWindow": 5000,
        "code_version": "abc",
        "model": "grok-4.5",
    }))
    g.write_text(json.dumps({"speech": {"target": "tui", "params": {}}}))
    t = telemetry_from_files("Trip-G", h, g, abide_head="def")
    assert t.context_pct == 20
    assert t.is_generating
    assert t.gaze_target == "tui"
    assert t.model_name == "grok-4.5"
    assert code_version_stale(t.code_version, "def")

def test_missing_files(tmp_path):
    t = telemetry_from_files("X", tmp_path/"no.json", tmp_path/"no2.json")
    assert t.health_status == "unknown"
    assert t.gaze_target == "unknown"
