"""Unit tests for entity_router.route()."""

from cora import entity_router as er
from cora.entity_router import is_mapped, matched_pattern, route


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


# --- Lexington Services — GM level ---


def test_lex_bare():
    """Bare #lex channel routes to LEX (regression: 2026-05-22 bug fix)."""
    assert route("lex") == "LEX"


def test_lex_clients():
    assert route("lex-clients") == "LEX"


def test_lex_leadership():
    """GM-level #lex-leadership still routes to LEX, not a sub-entity."""
    assert route("lex-leadership") == "LEX"


def test_lex_finance():
    assert route("lex-finance") == "LEX"


def test_lex_cora_build():
    assert route("lex-cora-build") == "LEX"


# --- Lexington LLC sub-entity (LEX-LLC) ---


def test_llc_bare():
    """Bare #llc routes to LEX-LLC (sub-entity siloing fix 2026-05-23)."""
    assert route("llc") == "LEX-LLC"


def test_llc_operations():
    assert route("llc-operations") == "LEX-LLC"


def test_llc_finance():
    assert route("llc-finance") == "LEX-LLC"


# --- Lexington Therapies sub-entity (LEX-LTS) ---


def test_lts_bare():
    """Bare #lts routes to LEX-LTS."""
    assert route("lts") == "LEX-LTS"


def test_lts_operations():
    assert route("lts-operations") == "LEX-LTS"


# --- Lexington Behavioral Health sub-entity (LEX-LBHS) ---


def test_lbhs_bare():
    """Bare #lbhs routes to LEX-LBHS."""
    assert route("lbhs") == "LEX-LBHS"


def test_lbhs_finance():
    assert route("lbhs-finance") == "LEX-LBHS"


# --- Lex Life Academy sub-entity (LEX-LLA) ---


def test_lla_bare():
    """Bare #lla routes to LEX-LLA."""
    assert route("lla") == "LEX-LLA"


def test_lla_leadership():
    assert route("lla-leadership") == "LEX-LLA"


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


# --- matched_pattern() / is_mapped() ---


def test_matched_pattern_explicit_route():
    """A real entity channel returns its own pattern, not the catch-all."""
    assert matched_pattern("osn-leadership") == "osn-*"


def test_matched_pattern_bare_entity():
    assert matched_pattern("f3e") == "f3e"


def test_matched_pattern_catchall_for_unknown():
    """A channel with no dedicated route matches only the trailing '*' catch-all."""
    assert matched_pattern("mystery-channel-xyz") == "*"


def test_is_mapped_true_for_routed_entity_channels():
    # Operational / sub channels that are well-routed but absent from
    # entity-channels.yaml -- the false-positive class the old monitor over-reported.
    for name in (
        "osn-recon-pilot",
        "f3-pure-launch",
        "bdm-osn",
        "llc-operations",
        "rogers-ranch-bookings",
        "polar-metrics",
        "clover-daily",
    ):
        assert is_mapped(name) is True, name


def test_is_mapped_true_for_explicit_fndr_and_silent_channels():
    """Explicit FNDR/HJRG + silent feed channels resolve to FNDR but ARE mapped."""
    for name in ("hjrg-leadership", "fndr", "fndr-general", "asana-feed", "general-do-not-use"):
        assert is_mapped(name) is True, name


def test_is_mapped_false_for_catchall_only():
    """Channels matching only the '*' catch-all are unmapped (no dedicated route)."""
    for name in ("mystery-channel", "some-unknown-channel-xyz", "random-2026-thing"):
        assert is_mapped(name) is False, name


def test_is_mapped_never_diverges_from_route_catchall():
    """is_mapped() False <=> route() reached the catch-all (not an explicit FNDR rule)."""
    # Unmapped -> route still returns FNDR (catch-all), but is_mapped is False.
    assert route("totally-unknown-zzz") == "FNDR"
    assert is_mapped("totally-unknown-zzz") is False
    # Explicit FNDR -> route returns FNDR AND is_mapped is True.
    assert route("hjrg") == "FNDR"
    assert is_mapped("hjrg") is True


def test_catchall_is_the_last_and_only_wildcard_route():
    """is_mapped() assumes the trailing '*' catch-all is the LAST route and the
    ONLY one. If a future YAML edit changes that, is_mapped() would silently
    misclassify every channel -- fail loudly here instead."""
    assert er._ROUTES[-1]["pattern"] == er._CATCHALL_PATTERN
    assert sum(1 for r in er._ROUTES if r["pattern"] == er._CATCHALL_PATTERN) == 1


def test_cora_ops_channels_are_mapped():
    """Cora's own operational channels resolve to FNDR via explicit cora-* route
    (not the catch-all), so the health monitor doesn't nag them as 'unmapped'."""
    for name in ("cora-build", "cora-health", "cora-filing", "cora-kb-log", "info-for-cora"):
        assert route(name) == "FNDR", name
        assert is_mapped(name) is True, name


def test_cora_kq_still_routes_to_its_entity_not_swallowed_by_cora_glob():
    """The cora-* route must NOT shadow the earlier explicit cora-kq-* routes."""
    assert route("cora-kq-f3e") == "F3E"
    assert route("cora-kq-lex-llc") == "LEX-LLC"
