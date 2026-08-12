import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))
from pathlib import Path
from tui_env import TuiEnv

def test_from_defaults(tmp_path):
    env = TuiEnv.from_defaults(str(tmp_path))
    assert env.agent_home("Sr") == tmp_path / "Sr"
    assert env.health_file("Sr") == tmp_path / "Sr" / "asdaaas" / "health.json"
    assert env.gaze_file("Trip") == tmp_path / "Trip" / "asdaaas" / "gaze.json"
