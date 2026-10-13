"""Family from nested homes; catalog labels."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tui"))

from agent_family import catalog_entries, catalog_label, family_from_home


def test_nested_levismith():
    root = Path("/home/eric/agents")
    assert family_from_home(root / "LeviSmith" / "Holt", agents_dir=root) == "LeviSmith"


def test_flat_mikeyv():
    root = Path("/home/eric/agents")
    assert family_from_home(root / "Sr", agents_dir=root) == "MikeyV"


def test_sixel():
    root = Path("/home/eric/agents")
    assert family_from_home(root / "Sixel" / "Astro", agents_dir=root) == "Sixel"


def test_label():
    assert catalog_label("LeviSmith", "Holt") == "LeviSmith / Holt"
    assert catalog_label("MikeyV", "Sr") == "Sr"


def test_catalog_sorted(tmp_path):
    (tmp_path / "LeviSmith" / "Holt" / "asdaaas").mkdir(parents=True)
    (tmp_path / "Sr" / "asdaaas").mkdir(parents=True)
    agents = {
        "Holt": {"home": str(tmp_path / "LeviSmith" / "Holt")},
        "Sr": {"home": str(tmp_path / "Sr")},
    }
    rows = catalog_entries(agents, agents_dir=tmp_path)
    assert [r.name for r in rows] == ["Holt", "Sr"]
    assert rows[0].label == "LeviSmith / Holt"
    assert rows[1].label == "Sr"
