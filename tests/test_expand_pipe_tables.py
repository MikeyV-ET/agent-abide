import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tui"))
from chat_widgets import expand_pipe_tables


def test_uuid_table_becomes_backticked_list():
    src = (
        "| uuid |\n"
        "|------|\n"
        "| 01a1366b-0432-7220-8079-8311f4c98e81 |\n"
        "| 01a1366b-0432-7220-8079-8311f4c98e82 |\n"
    )
    out = expand_pipe_tables(src)
    assert "…" not in out and "..." not in out
    assert "01a1366b-0432-7220-8079-8311f4c98e81" in out
    assert out.count("`") >= 4
    assert "|" not in out.split("\n")[0]


def test_two_col_table_keeps_headers():
    src = (
        "| name | id |\n"
        "|------|----|\n"
        "| Holt | 01a1366b-0432-7220-8079-8311f4c98e81 |\n"
    )
    out = expand_pipe_tables(src)
    assert "Holt" in out
    assert "01a1366b-0432-7220-8079-8311f4c98e81" in out
    assert "**name:**" in out or "name:" in out


def test_prose_with_pipe_untouched():
    src = "use `|` for tables, not ellipsis"
    assert expand_pipe_tables(src) == src
