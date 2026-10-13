from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from operator_flag import derive_tab_status, tab_status_for, write_flag


def test_priority_error_beats_waiting():
    assert (
        derive_tab_status(
            flag={"status": "waiting"},
            binary_state={"state": "STUCK"},
        )
        == "error"
    )


def test_waiting_beats_working():
    assert (
        derive_tab_status(
            flag={"status": "waiting"},
            health={"status": "working", "observer": {"state": "BUSY"}},
        )
        == "waiting"
    )


def test_busy_is_working_without_flag():
    assert derive_tab_status(binary_state={"state": "BUSY"}) == "working"


def test_idle_default():
    assert derive_tab_status() == "idle"


def test_doom_loop_is_error():
    assert derive_tab_status(binary_state={"state": "BUSY", "doom_loop": True}) == "error"


def test_write_clear(tmp_path):
    write_flag(tmp_path, "waiting", note="need you")
    assert tab_status_for(tmp_path) == "waiting"
    write_flag(tmp_path, "clear")
    assert not (tmp_path / "operator_flag.json").exists()
    assert tab_status_for(tmp_path) == "idle"
