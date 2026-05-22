"""Unit tests for entity_router.route()."""

from cora.entity_router import route


# --- F3 Energy ---


def test_f3e_bare():
    """Bare #f3e channel routes to F3E (regression: 2026-05-22 bug fix)."""
    assert route("f3e") == "F3E"


def test_f3e_leadership():
    assert route("f3e-leadership") == "F3E"


def test_f3_pure_launch():
    assert route("f3-pure-launch") == "F3E"


def test_polar_metrics():
    """Non-standard naming: F3E ad-spend dashboard channel."""
    assert route("polar-metrics") == "F3E"


# --- Lexington Services ---


def test_lex_bare():
    """Bare #lex channel routes to LEX (regression: 2026-05-22 bug fix)."""
    assert route("lex") == "LEX"


def test_lex_clients():
    assert route("lex-clients") == "LEX"


# --- One Stop Nutrition ---


def test_osn_bare():
    """Bare #osn channel routes to OSN (regression: 2026-05-22 bug fix)."""
    assert route("osn") == "OSN"


def test_osn_leadership():
    assert route("osn-leadership") == "OSN"


def test_osn_recon_pilot():
    assert route("osn-recon-pilot") == "OSN"


def test_clover_daily():
    """Non-standard naming: OSN cross-store daily sales summary."""
    assert route("clover-daily") == "OSN"


# --- Big D Media ---


def test_bdm_bare():
    """Bare #bdm channel routes to BDM (regression: 2026-05-22 bug fix)."""
    assert route("bdm") == "BDM"


def test_bdm_leadership():
    assert route("bdm-leadership") == "BDM"


# --- HJR Properties ---


def test_hjrp_bare():
    """Bare #hjrp channel routes to HJRP (regression: 2026-05-22 evening add)."""
    assert route("hjrp") == "HJRP"


def test_hjrp_finance():
    """#hjrp-finance routes to HJRP."""
    assert route("hjrp-finance") == "HJRP"


def test_rogers_ranch_bare():
    """Bare #rogers-ranch (HJRP-RR sub-entity catch-all) routes to HJRP."""
    assert route("rogers-ranch") == "HJRP"


def test_rogers_ranch_bookings():
    """#rogers-ranch-bookings routes to HJRP."""
    assert route("rogers-ranch-bookings") == "HJRP"


# --- HJR Global / Founder-level ---


def test_hjrg_bare():
    """Bare #hjrg channel routes to FNDR."""
    assert route("hjrg") == "FNDR"


def test_hjrg_leadership():
    assert route("hjrg-leadership") == "FNDR"


def test_fndr_exact():
    assert route("fndr") == "FNDR"


def test_fndr_prefix():
    assert route("fndr-general") == "FNDR"


# --- Catch-all ---


def test_random_channel_defaults_to_fndr():
    assert route("random-channel") == "FNDR"


def test_unknown_channel_defaults_to_fndr():
    assert route("some-unknown-channel-xyz") == "FNDR"
