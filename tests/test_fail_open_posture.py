"""Phase 2.6 -- pin the access posture so it can't silently drift.

Two intentional, load-bearing postures:

  FAIL-OPEN (convenience): an UNKNOWN Slack user (not in user-permissions.yaml)
  and an UNKNOWN channel resolve to the FNDR/HJRG aggregator scope -- the
  cross-entity overview, NOT any single entity's detail. This is deliberate
  (org-roles is advisory only; unknown askers get the pre-Phase-1 behavior).

  FAIL-CLOSED (security): every HARD guard denies by default regardless of who
  is asking -- an unknown user can NEVER relax PHI, the sibling firewall, the
  cross-entity firewall, or the finance tier. These run in code before any LLM
  call and are user-independent (or custodian-allowlisted).

If either posture flips, these tests fail -- which is the point.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

from cora import user_access, entity_router, lex_phi_access  # noqa: E402
from cora import cross_entity_guard, sibling_guard, channel_classifier  # noqa: E402

# A Slack ID that is intentionally not in any map.
_UNKNOWN = "U_UNKNOWN_NOBODY_999"


# ── FAIL-OPEN: unknown user / channel -> FNDR aggregator only ─────────────────
def test_unknown_user_is_authorized_for_aggregator_only():
    assert user_access.is_authorized(_UNKNOWN, "FNDR") is True
    assert user_access.is_authorized(_UNKNOWN, "HJRG") is True
    # ...but NOT for any single entity's detail.
    for entity in ("F3E", "LEX", "LEX-LLC", "OSN", "BDM", "UFL", "HJRP"):
        assert user_access.is_authorized(_UNKNOWN, entity) is False, entity


def test_unknown_channel_routes_to_fndr():
    assert entity_router.route("totally-unknown-channel-xyz") == "FNDR"
    assert entity_router.route("some-random-name") == "FNDR"


def test_unknown_user_passes_access_check_for_fndr_blocked_for_entity():
    # FNDR scope: allowed, no blocked topics for an unknown user.
    assert user_access.check_access(_UNKNOWN, "FNDR", "how is the portfolio doing?") is None
    # Entity scope they aren't authorized for: redirected (but the redirect must
    # NOT leak an internal entity code -- the 2026-06-01 #f3-events regression).
    redirect = user_access.check_access(_UNKNOWN, "F3E", "how are F3E retail sales?")
    assert redirect is not None
    assert "F3E" not in redirect and "FNDR" not in redirect


# ── FAIL-CLOSED: PHI custodian gate ───────────────────────────────────────────
def test_phi_gate_fails_closed_for_unknown_user():
    assert lex_phi_access.phi_allowed(_UNKNOWN, "LEX") is False
    assert lex_phi_access.phi_allowed(_UNKNOWN, "LEX-LLC") is False
    # Even a DM from an unknown (non-custodian) user is denied.
    assert lex_phi_access.phi_allowed(_UNKNOWN, "LEX", is_dm=True) is False
    # Empty / unresolved -> denied.
    assert lex_phi_access.phi_allowed("", None) is False


def test_phi_topic_block_not_relaxed_without_custodian_flag():
    # An unknown user asking a PHI question in LEX is refused at the entity gate;
    # the phi_custodian flag defaults False so the topic block also holds.
    refusal = user_access.check_access(_UNKNOWN, "LEX", "what's the client diagnosis?")
    assert refusal is not None


# ── FAIL-CLOSED: cross-entity firewall (user-independent) ─────────────────────
def test_cross_entity_guard_fires_regardless_of_user():
    # No user is even passed -- the guard is content+channel based, so an unknown
    # asker cannot relax it.
    assert cross_entity_guard.check_cross_entity(
        "what's F3 Energy's monthly revenue?", "OSN"
    ) is not None
    # FNDR/HJRG are the only pass-through aggregators.
    assert cross_entity_guard.check_cross_entity(
        "what's F3 Energy's monthly revenue?", "FNDR"
    ) is None


# ── FAIL-CLOSED: sibling firewall ─────────────────────────────────────────────
def test_sibling_guard_fires_for_lex_sub_entity_crosstalk():
    # An LLC channel asking about a sibling sub-entity (LLA) is redirected.
    assert sibling_guard.check_redirect("LEX-LLC", "What's LLA enrollment?") is not None
    # The GM-level LEX entity sees all sub-entities -> no redirect.
    assert sibling_guard.check_redirect("LEX", "What's LLA enrollment?") is None


# ── FAIL-CLOSED: finance tier ─────────────────────────────────────────────────
def test_finance_tier_closed_in_general_channels():
    # TIER_3 (ops/general/etc.) channels do not permit financial discussion;
    # the per-tool check in tool_dispatch refuses regardless of asker.
    assert channel_classifier.is_tier_1("F3E", "ops") is False
    assert channel_classifier.is_tier_1("F3E", "clients") is False
    # TIER_1 surfaces (finance/leadership) permit it.
    assert channel_classifier.is_tier_1("F3E", "finance") is True
    assert channel_classifier.is_tier_1("F3E", "leadership") is True
