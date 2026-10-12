"""simple n=1 must not require JUDGE_PROMPT / phase C."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUERY = (ROOT / "scripts" / "memory_query.py").read_text()


def test_simple_path_has_merge_not_unconditional_phase_c():
    # Regression: simple always called prepare-c then phase c; skip-peer never
    # writes JUDGE_PROMPT.md → no_JUDGE_PROMPT (Squiggy 2026-10-12).
    assert "if args.peer:" in QUERY
    assert 'synthesize.py"), "merge"' in QUERY
    # phase c spawn only inside the peer branch
    peer_idx = QUERY.find("if args.peer:")
    c_idx = QUERY.find('--phase", "c"')
    assert peer_idx != -1 and c_idx != -1
    assert c_idx > peer_idx
