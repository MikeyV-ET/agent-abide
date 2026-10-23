from pathlib import Path
import json
import time

from floor_flag import write_floor, clear_floor, read_floor, raised_in_room


def test_raise_and_order(tmp_path: Path):
    a = tmp_path / "Holt" / "asdaaas"
    b = tmp_path / "Squiggy" / "asdaaas"
    write_floor(a, "#Gen4", agent="Holt")
    time.sleep(0.02)
    write_floor(b, "#gen4", agent="Squiggy")
    def home(name):
        return tmp_path / name / "asdaaas"
    assert raised_in_room("#gen4", ["Holt", "Squiggy"], home) == ["Holt", "Squiggy"]
    assert read_floor(a)["room"] == "#gen4"
    clear_floor(a)
    assert raised_in_room("#gen4", ["Holt", "Squiggy"], home) == ["Squiggy"]


def test_clear_missing(tmp_path: Path):
    clear_floor(tmp_path / "nope")
    assert read_floor(tmp_path / "nope") is None
