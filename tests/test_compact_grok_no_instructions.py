"""Grok agent stdio /compact takes no preserve-text."""


def test_grok_compact_prompt_is_bare():
    from turn_engine import TurnEngine

    class GrokBackend:
        pass

    te = TurnEngine.__new__(TurnEngine)
    te.backend = GrokBackend()
    te.agent_name = "Squiggy"
    te.env = None
    assert te._compact_prompt_for_backend({"instructions": "Keep the mill."}) == "/compact"
    assert te._compact_prompt_for_backend({}) == "/compact"


def test_non_grok_still_appends_instructions():
    from turn_engine import TurnEngine

    class OtherBackend:
        pass

    te = TurnEngine.__new__(TurnEngine)
    te.backend = OtherBackend()
    te.agent_name = "Squiggy"
    te.env = None
    p = te._compact_prompt_for_backend({"instructions": "Keep the mill."})
    assert p.startswith("/compact ")
    assert "Keep the mill." in p
