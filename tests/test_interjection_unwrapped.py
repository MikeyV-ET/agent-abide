
from chat_model import extract_interjections

def test_extract_empty_without_tags():
    clean, msgs = extract_interjections("[Puppet (via tui)] Smoke test: 18 x 19")
    assert msgs == []
    assert "Puppet" in clean
