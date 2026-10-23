from pathlib import Path
import irc_round as ir


def test_format_round_line(tmp_path):
    p = tmp_path / "irc_round.json"
    ir.write_round("#Gen4", order=["Squiggy", "Holt"], seat="Squiggy", flags=["Holt"], path=p)
    row = ir.read_round("#gen4", path=p)
    line = ir.format_round_line(row)
    assert "Squiggy*" in line
    assert "Holt" in line
    assert "flags Holt" in line
    ir.clear_round("#gen4", path=p)
    assert ir.read_round("#gen4", path=p) is None
