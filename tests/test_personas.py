import re

from app import personas


def test_roster_unique_dns_safe_names():
    names = personas.GUEST_NAMES
    # Roster must cover the default GUEST_COUNT (12) with headroom for
    # runtime "Add guest".
    assert len(names) >= 12
    assert len(set(names)) == len(names)
    # Guests are addressed as <name>.hotel.actors... — must be DNS labels.
    for name in names:
        assert re.fullmatch(r"[a-z]([a-z0-9-]*[a-z0-9])?", name), name


def test_canned_reply_references_first_message():
    history = [
        {"role": "user", "text": "my favorite color is teal"},
        {"role": "assistant", "text": "noted!"},
    ]
    reply = personas.canned_reply("amara", "what is my favorite color?", history)
    # The memory proof works even without an LLM: the canned reply quotes the
    # first user message, which only checkpointed RAM could know.
    assert "my favorite color is teal" in reply
    assert "reply #2" in reply


def test_unknown_guest_gets_generic_persona():
    p = personas.get("stranger")
    assert p.name == "stranger"
    assert "stranger" in p.system_prompt
