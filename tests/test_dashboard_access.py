"""Tests for the deterministic dashboard -> Slack-surface access guard.

Runs the fail-closed matrix against the REAL shipped data/maps/dashboard-access.yaml
(so it pins the actual config), plus monkeypatched fail-closed / edge cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cora import dashboard_access
from cora.dashboard_access import check_dashboard_access

HARRISON = "U0B2RM2JYJ1"
OTHER = "U0BSOMEONEELSE"

ONEAMERICA = "oneamerica-whole-life-portfolio"
CAPITAL = "f3-capital-program"
CREATOR = "f3-creator-sponsorship-command-center"
CONTENT = "f3-content-pipeline"

# Substrings a refusal must NEVER contain (no-existence-leak): no dashboard id,
# store, platform, sub-brand, or allowed-channel name.
_LEAK_TERMS = [
    "airtable", "notion", "drive", "oneamerica", "one america", "shopify",
    "capital", "policy", "insurance", "creator", "content pipeline", "roster",
    "f3-athletes", "f3e-leadership", "founder-operations", "app", "tbl",
    "1ini4", "1bzi6", "1npb",
]


def _assert_no_leak(msg: str) -> None:
    assert msg, "refusal must be a non-empty string"
    low = msg.lower()
    for term in _LEAK_TERMS:
        assert term not in low, f"refusal leaked {term!r}: {msg!r}"


@pytest.fixture(autouse=True)
def _clean_cache():
    dashboard_access.invalidate_cache()
    yield
    dashboard_access.invalidate_cache()


# --------------------------------------------------------------------------- #
# Personal dashboards: DM-to-Harrison is the ONLY pass.                        #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("dash", [ONEAMERICA, CAPITAL])
def test_personal_dm_harrison_passes(dash):
    assert check_dashboard_access(dash, HARRISON, "dm") is None


@pytest.mark.parametrize("dash", [ONEAMERICA, CAPITAL])
def test_personal_dm_non_harrison_refused(dash):
    msg = check_dashboard_access(dash, OTHER, "dm")
    assert msg is not None
    _assert_no_leak(msg)


@pytest.mark.parametrize("dash", [ONEAMERICA, CAPITAL])
@pytest.mark.parametrize("chan", ["cora-build", "f3e-leadership", "f3-athletes", "osn-leadership", "founder-operations"])
def test_personal_in_any_named_channel_refused(dash, chan):
    # Even Harrison, even in a founder channel: personal dashboards never surface
    # outside a DM.
    msg = check_dashboard_access(dash, HARRISON, chan)
    assert msg is not None
    _assert_no_leak(msg)


def test_personal_named_channel_uses_dm_copy():
    # The kickoff negative: OneAmerica asked in #cora-build -> generic personal refusal.
    msg = check_dashboard_access(ONEAMERICA, HARRISON, "cora-build")
    assert msg == "I don't have that here -- ask me in a DM."


# --------------------------------------------------------------------------- #
# Creator CRM (ENTITY F3E): named F3E channels + founder channels + Harrison DM.#
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("chan", ["f3-athletes", "f3e-leadership", "founder-operations"])
def test_creator_allowed_channels_pass(chan):
    assert check_dashboard_access(CREATOR, OTHER, chan) is None


def test_creator_mapped_fndr_channel_passes():
    # #cora-build routes to FNDR (mapped via cora-*) and FNDR is in allow_entities.
    assert check_dashboard_access(CREATOR, OTHER, "cora-build") is None


def test_creator_harrison_dm_passes():
    assert check_dashboard_access(CREATOR, HARRISON, "dm") is None


def test_creator_non_harrison_dm_refused():
    msg = check_dashboard_access(CREATOR, OTHER, "dm")
    assert msg is not None
    _assert_no_leak(msg)


def test_creator_osn_channel_refused():
    # Negative: an OSN channel routes to OSN (not in allow_entities/channels).
    msg = check_dashboard_access(CREATOR, OTHER, "osn-leadership")
    assert msg == "That's not available in this channel."


def test_creator_other_f3e_channel_refused():
    # #f3-ai-visibility routes to F3E but F3E is NOT in allow_entities and the
    # channel isn't named -> refuse. Proves it is NOT "any F3E channel".
    msg = check_dashboard_access(CREATOR, OTHER, "f3-ai-visibility")
    assert msg is not None
    _assert_no_leak(msg)


# --------------------------------------------------------------------------- #
# Content pipeline (FOUNDER_OPS): founder channels + Harrison DM.              #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("chan", ["founder-operations", "cora-build", "hjrg-leadership", "fndr-decisions"])
def test_content_founder_channels_pass(chan):
    assert check_dashboard_access(CONTENT, OTHER, chan) is None


def test_content_harrison_dm_passes():
    assert check_dashboard_access(CONTENT, HARRISON, "dm") is None


def test_content_f3e_channel_refused():
    # An F3E channel is not a founder channel -> content pipeline refused there.
    msg = check_dashboard_access(CONTENT, OTHER, "f3-athletes")
    assert msg is not None
    _assert_no_leak(msg)


def test_content_osn_channel_refused():
    assert check_dashboard_access(CONTENT, OTHER, "osn-leadership") is not None


# --------------------------------------------------------------------------- #
# Fail-open guard: unmapped/unknown/empty channels never pass a founder dash.  #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("dash", [CREATOR, CONTENT])
def test_unmapped_channel_refused(dash):
    # A random unmapped channel routes to FNDR via the catch-all, but is_mapped()
    # is False -> must NOT fall through into a founder-scoped dashboard.
    msg = check_dashboard_access(dash, OTHER, "random-unmapped-xyz-123")
    assert msg is not None
    _assert_no_leak(msg)


@pytest.mark.parametrize("dash", [ONEAMERICA, CAPITAL, CREATOR, CONTENT])
def test_empty_channel_refused(dash):
    assert check_dashboard_access(dash, HARRISON, "") is not None


@pytest.mark.parametrize("dash", [ONEAMERICA, CAPITAL, CREATOR, CONTENT])
def test_none_channel_refused(dash):
    assert check_dashboard_access(dash, HARRISON, None) is not None


def test_unknown_dashboard_refused():
    msg = check_dashboard_access("no-such-dashboard", HARRISON, "dm")
    assert msg is not None
    _assert_no_leak(msg)


def test_channel_name_normalization_hash_and_case():
    # A leading '#' or upper-case must not defeat the allow list.
    assert check_dashboard_access(CREATOR, OTHER, "#F3-Athletes") is None
    assert check_dashboard_access(CREATOR, HARRISON, "DM") is None


# --------------------------------------------------------------------------- #
# Fail-closed: missing / unparseable YAML => every dashboard refuses.          #
# --------------------------------------------------------------------------- #
def test_missing_yaml_all_refuse(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard_access, "_ACCESS_PATH", tmp_path / "nope.yaml")
    dashboard_access.invalidate_cache()
    for dash in [ONEAMERICA, CAPITAL, CREATOR, CONTENT]:
        assert check_dashboard_access(dash, HARRISON, "dm") is not None


def test_unparseable_yaml_all_refuse(monkeypatch, tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("dashboards: [this is: not valid: mapping::\n  - broken", encoding="utf-8")
    monkeypatch.setattr(dashboard_access, "_ACCESS_PATH", bad)
    dashboard_access.invalidate_cache()
    assert check_dashboard_access(CREATOR, HARRISON, "f3-athletes") is not None


def test_empty_dashboards_not_cached(monkeypatch, tmp_path):
    # An empty map must not be cached (never-cache-empty): a later good load wins.
    empty = tmp_path / "empty.yaml"
    empty.write_text("dashboards: {}\n", encoding="utf-8")
    monkeypatch.setattr(dashboard_access, "_ACCESS_PATH", empty)
    dashboard_access.invalidate_cache()
    assert check_dashboard_access(CREATOR, OTHER, "f3-athletes") is not None
    assert dashboard_access._cache == {}  # nothing cached


def test_real_yaml_loads_and_caches():
    # Sanity: the shipped file parses and yields the five dashboards.
    m = dashboard_access._load_access_map()
    for dash in [ONEAMERICA, CAPITAL, CREATOR, CONTENT, "travel-points-optimizer"]:
        assert dash in m


def test_shipped_file_exists():
    assert (Path(dashboard_access._ACCESS_PATH)).exists()
