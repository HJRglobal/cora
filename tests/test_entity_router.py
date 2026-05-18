"""Unit tests for entity_router.route()."""

from cora.entity_router import route


def test_f3e_leadership():
    assert route("f3e-leadership") == "F3E"


def test_f3_pure_launch():
    assert route("f3-pure-launch") == "F3E"


def test_lex_clients():
    assert route("lex-clients") == "LEX"


def test_fndr_exact():
    assert route("fndr") == "FNDR"


def test_fndr_prefix():
    assert route("fndr-general") == "FNDR"


def test_random_channel_defaults_to_fndr():
    assert route("random-channel") == "FNDR"


def test_unknown_channel_defaults_to_fndr():
    assert route("some-unknown-channel-xyz") == "FNDR"
