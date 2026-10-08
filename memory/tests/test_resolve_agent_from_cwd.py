"""Nested agent homes must not resolve as the parent directory name."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from memory_service_mcp.server import resolve_agent_from_cwd


def test_nested_squiggy_home():
    agents = {
        "Trip-G": {"home": "/home/eric/agents/Trip-G"},
        "Squiggy": {"home": "/home/eric/agents/LeviSmith/Squiggy"},
        "Wend": {"home": "/home/eric/agents/LeviSmith/Wend"},
    }
    hit = resolve_agent_from_cwd(
        Path("/home/eric/agents/LeviSmith/Squiggy"), agents
    )
    assert hit["agent"] == "Squiggy"
    assert hit["home"].endswith("LeviSmith/Squiggy")


def test_nested_home_subdir_still_squiggy():
    agents = {
        "Squiggy": {"home": "/home/eric/agents/LeviSmith/Squiggy"},
    }
    hit = resolve_agent_from_cwd(
        Path("/home/eric/agents/LeviSmith/Squiggy/asdaaas"), agents
    )
    assert hit["agent"] == "Squiggy"


def test_parent_levismith_is_not_an_agent():
    agents = {
        "Squiggy": {"home": "/home/eric/agents/LeviSmith/Squiggy"},
        "Trip-G": {"home": "/home/eric/agents/Trip-G"},
    }
    assert resolve_agent_from_cwd(Path("/home/eric/agents/LeviSmith"), agents) is None


def test_tripg_top_level():
    agents = {
        "Trip-G": {"home": "/home/eric/agents/Trip-G"},
        "Squiggy": {"home": "/home/eric/agents/LeviSmith/Squiggy"},
    }
    hit = resolve_agent_from_cwd(Path("/home/eric/agents/Trip-G"), agents)
    assert hit["agent"] == "Trip-G"
