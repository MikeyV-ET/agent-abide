import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tui"))
from chat_model import extract_interjections, interjection_key

SAMPLE = """stdout...
<interjection>
[system: messages arrived during your tool call]
[eric (via tui) (id=bell_32ac0874, ts=Tue Aug 04 12:49 PDT)] also, here's what I ran: foo
</interjection>
more stdout
"""

def test_extract_once_per_block():
    clean, msgs = extract_interjections(SAMPLE)
    assert len(msgs) == 1
    assert "bell_32ac0874" in msgs[0] or "also, here's" in msgs[0]
    assert "<interjection>" not in clean
    assert "more stdout" in clean

def test_key_stable_across_growing_output():
    m1 = extract_interjections(SAMPLE)[1][0]
    bigger = SAMPLE + "\n" + "x" * 5000
    m2 = extract_interjections(bigger)[1][0]
    assert interjection_key(m1) == interjection_key(m2) == "bell:bell_32ac0874"

def test_dedup_set_simulates_multi_tool():
    """Same interjection in 8 tool updates → one key."""
    seen = set()
    mounted = 0
    for _ in range(8):
        _, msgs = extract_interjections(SAMPLE)
        for msg in msgs:
            k = interjection_key(msg)
            if k in seen:
                continue
            seen.add(k)
            mounted += 1
    assert mounted == 1
