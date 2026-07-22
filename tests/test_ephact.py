"""Tests for ephemeral artifact (ephact) parser, viewer, and archive."""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tui'))

import pytest

from ephact_parser import extract_ephacts, has_partial_ephact, EphactData
from ephact_viewer import EphactViewer, EphactEntry, archive_ephact


# ── Parser tests ──────────────────────────────────────────────────────

class TestExtractEphacts:
    def test_basic_table(self):
        text = 'Here is a table:\n<ephact type="table" title="Tasks">\n| A | B |\n|---|---|\n| 1 | 2 |\n</ephact>\nDone.'
        cleaned, ephacts = extract_ephacts(text)
        assert len(ephacts) == 1
        assert ephacts[0].type == "table"
        assert ephacts[0].title == "Tasks"
        assert "| A | B |" in ephacts[0].content
        assert "<ephact" not in cleaned
        assert "Done." in cleaned

    def test_no_title(self):
        text = '<ephact type="paragraph">Hello world</ephact>'
        cleaned, ephacts = extract_ephacts(text)
        assert len(ephacts) == 1
        assert ephacts[0].title is None
        assert ephacts[0].content == "Hello world"
        assert cleaned.strip() == ""

    def test_multiple_ephacts(self):
        text = 'A <ephact type="list">- x\n- y</ephact> B <ephact type="code">foo()</ephact> C'
        cleaned, ephacts = extract_ephacts(text)
        assert len(ephacts) == 2
        assert ephacts[0].type == "list"
        assert ephacts[1].type == "code"
        assert "A" in cleaned and "B" in cleaned and "C" in cleaned
        assert "<ephact" not in cleaned

    def test_no_ephacts(self):
        text = "Just plain text with no tags."
        cleaned, ephacts = extract_ephacts(text)
        assert len(ephacts) == 0
        assert cleaned == text

    def test_partial_unclosed_tag(self):
        text = "Starting: <ephact type=\"table\">| col1 |"
        cleaned, ephacts = extract_ephacts(text)
        assert len(ephacts) == 0
        assert "<ephact" in cleaned  # left intact

    def test_single_quotes(self):
        text = "<ephact type='code' title='Snippet'>x = 1</ephact>"
        cleaned, ephacts = extract_ephacts(text)
        assert len(ephacts) == 1
        assert ephacts[0].type == "code"
        assert ephacts[0].title == "Snippet"

    def test_multiline_content(self):
        text = '<ephact type="table">\n| H1 | H2 |\n|---|---|\n| a | b |\n| c | d |\n</ephact>'
        _, ephacts = extract_ephacts(text)
        assert len(ephacts) == 1
        assert "| a | b |" in ephacts[0].content
        assert "| c | d |" in ephacts[0].content

    def test_extra_blank_lines_cleaned(self):
        text = "Before\n\n\n<ephact type=\"list\">- x</ephact>\n\n\nAfter"
        cleaned, _ = extract_ephacts(text)
        assert "\n\n\n" not in cleaned

    def test_inline_code_in_ephact_content(self):
        text = '<ephact type="table" title="T">| col |\n|---|\n| `pass` | `fail` |</ephact>'
        cleaned, ephacts = extract_ephacts(text)
        assert len(ephacts) == 1
        assert "`pass`" in ephacts[0].content
        assert "`fail`" in ephacts[0].content
        assert "MASK" not in ephacts[0].content


class TestHasPartialEphact:
    def test_no_tags(self):
        assert has_partial_ephact("no tags") is False

    def test_complete_tag(self):
        assert has_partial_ephact('<ephact type="x">y</ephact>') is False

    def test_unclosed(self):
        assert has_partial_ephact('<ephact type="x">partial content') is True

    def test_multiple_one_unclosed(self):
        text = '<ephact type="a">done</ephact> then <ephact type="b">still going'
        assert has_partial_ephact(text) is True


# ── Viewer tests (non-rendering) ─────────────────────────────────────

class TestEphactViewer:
    @pytest.fixture
    def viewer(self):
        v = EphactViewer()
        v._active_agent = "Trip"
        return v

    def test_push_and_current(self, viewer):
        e = EphactData(type="table", content="| A |", title="T1")
        viewer.push("Trip", e)
        assert viewer.has_content
        assert viewer.current_entry.data.type == "table"
        assert viewer._visible is True

    def test_stack_order(self, viewer):
        viewer.push("Trip", EphactData(type="table", content="first"))
        viewer.push("Trip", EphactData(type="list", content="second"))
        assert viewer.current_entry.data.content == "second"  # newest

    def test_navigate_wraps(self, viewer):
        viewer.push("Trip", EphactData(type="a", content="1"))
        viewer.push("Trip", EphactData(type="b", content="2"))
        viewer.push("Trip", EphactData(type="c", content="3"))
        assert viewer.current_entry.data.type == "c"
        viewer.navigate(-1)
        assert viewer.current_entry.data.type == "b"
        viewer.navigate(-1)
        assert viewer.current_entry.data.type == "a"
        viewer.navigate(-1)  # wraps to end
        assert viewer.current_entry.data.type == "c"
        viewer.navigate(1)  # wraps to start
        assert viewer.current_entry.data.type == "a"

    def test_per_agent_isolation(self, viewer):
        viewer.push("Trip", EphactData(type="table", content="trip's"))
        viewer.push("Jr", EphactData(type="code", content="jr's"))
        viewer.set_active_agent("Jr")
        assert viewer.current_entry.data.content == "jr's"
        viewer.set_active_agent("Trip")
        assert viewer.current_entry.data.content == "trip's"

    def test_close(self, viewer):
        viewer.push("Trip", EphactData(type="x", content="y"))
        assert viewer._visible is True
        viewer.close()
        assert viewer._visible is False

    def test_empty_agent(self, viewer):
        viewer.set_active_agent("Q")  # no ephacts
        assert viewer.has_content is False
        assert viewer.current_entry is None

    def test_close_current_removes_entry(self, viewer):
        viewer.push("Trip", EphactData(type="a", content="1"))
        viewer.push("Trip", EphactData(type="b", content="2"))
        viewer.push("Trip", EphactData(type="c", content="3"))
        # Viewing "c" (index 2), remove it
        viewer.close_current()
        assert len(viewer._stacks["Trip"]) == 2
        assert viewer.current_entry.data.type == "b"

    def test_close_current_last_hides_viewer(self, viewer):
        viewer.push("Trip", EphactData(type="only", content="x"))
        assert viewer._visible is True
        viewer.close_current()
        assert viewer._visible is False
        assert len(viewer._stacks["Trip"]) == 0

    def test_close_current_adjusts_index(self, viewer):
        viewer.push("Trip", EphactData(type="a", content="1"))
        viewer.push("Trip", EphactData(type="b", content="2"))
        # Navigate to first, then close it
        viewer._view_index["Trip"] = 0
        viewer.close_current()
        assert viewer.current_entry.data.type == "b"
        assert viewer._view_index["Trip"] == 0


# ── Archive tests ─────────────────────────────────────────────────────

class TestArchive:
    def test_archive_writes_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            entry = EphactEntry(
                data=EphactData(type="table", content="| A |", title="Tasks"),
                agent="Trip",
                timestamp=1234567890.123,
            )
            path = archive_ephact("Trip", entry, agents_home=tmpdir)
            assert path.exists()
            data = json.loads(path.read_text())
            assert data["type"] == "table"
            assert data["title"] == "Tasks"
            assert data["content"] == "| A |"
            assert data["agent"] == "Trip"
            assert data["timestamp"] == 1234567890.123

    def test_archive_creates_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            entry = EphactEntry(
                data=EphactData(type="list", content="- a"),
                agent="Jr",
            )
            path = archive_ephact("Jr", entry, agents_home=tmpdir)
            assert (Path(tmpdir) / "Jr" / "ephacts").is_dir()
            assert path.exists()

    def test_archive_multiple(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import time
            e1 = EphactEntry(data=EphactData(type="a", content="1"), agent="Trip", timestamp=1000.0)
            e2 = EphactEntry(data=EphactData(type="b", content="2"), agent="Trip", timestamp=2000.0)
            p1 = archive_ephact("Trip", e1, agents_home=tmpdir)
            p2 = archive_ephact("Trip", e2, agents_home=tmpdir)
            assert p1 != p2
            ephacts_dir = Path(tmpdir) / "Trip" / "ephacts"
            assert len(list(ephacts_dir.glob("ephact_*.json"))) == 2
