from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tui"))
from multiway import is_mw_tab, members_from_tab, new_jsonl_records, parse_members, tab_id


def test_parse_and_tab():
    m = parse_members("Holt, Squiggy")
    assert m == ["Holt", "Squiggy"]
    assert tab_id(m) == "#mw-Holt-Squiggy"
    assert is_mw_tab("#mw-Holt-Squiggy")
    assert members_from_tab("#mw-Holt-Squiggy") == ["Holt", "Squiggy"]


def test_jsonl_tail(tmp_path):
    p = tmp_path / "speech.jsonl"
    p.write_text(json.dumps({"role": "assistant", "content": "hi"}) + "\n")
    rows, off = new_jsonl_records(p, 0)
    assert len(rows) == 1 and rows[0]["content"] == "hi"
    p.open("a").write(json.dumps({"role": "assistant", "content": "there"}) + "\n")
    rows2, off2 = new_jsonl_records(p, off)
    assert [r["content"] for r in rows2] == ["there"]
    assert off2 > off
