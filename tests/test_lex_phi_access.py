"""Tests for the LEX PHI custodian access gate (lex_phi_access.py, 2026-06-09).

Compliance-critical. Verifies the fail-closed allowlist matrix, the check_access
integration (phi_custodian only relaxes the `phi` block, nothing else), and that
the cross-entity guard still blocks LEX content outside LEX scope regardless.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import lex_phi_access as lpa  # noqa: E402
from cora import user_access  # noqa: E402
from cora import cross_entity_guard  # noqa: E402

HARRISON = "U0B2RM2JYJ1"
SHAUN = "U0B3PS82G30"
JEN = "U0B3VGT8RE0"
JEFF = "U0B3KHBJJ91"
RANDO = "U0XXXXXXXXX"


@pytest.fixture(autouse=True)
def _reset_lpa_cache():
    """Reset the module TTL cache before each test for isolation."""
    lpa._cache = frozenset()
    lpa._loaded_at = 0.0
    yield
    lpa._cache = frozenset()
    lpa._loaded_at = 0.0


# ---------------------------------------------------------------------------
# Allowlist membership (reads the real data/maps/lex-phi-custodians.yaml)
# ---------------------------------------------------------------------------

def test_real_custodians_are_recognized():
    for uid in (HARRISON, SHAUN, JEN, JEFF):
        assert lpa.is_custodian(uid) is True


def test_non_custodian_rejected():
    assert lpa.is_custodian(RANDO) is False
    assert lpa.is_custodian("") is False


# ---------------------------------------------------------------------------
# phi_allowed matrix
# ---------------------------------------------------------------------------

def test_custodian_in_lex_channel_allowed():
    assert lpa.phi_allowed(SHAUN, "LEX") is True
    assert lpa.phi_allowed(JEN, "LEX-LLC") is True
    assert lpa.phi_allowed(HARRISON, "LEX-LBHS") is True


def test_custodian_in_non_lex_channel_refused():
    # The spec's hard rule: LEX PHI never surfaces outside LEX scope, even for a custodian.
    assert lpa.phi_allowed(SHAUN, "F3E") is False
    assert lpa.phi_allowed(SHAUN, "FNDR") is False
    assert lpa.phi_allowed(HARRISON, "OSN") is False


def test_custodian_dm_allowed():
    assert lpa.phi_allowed(JEFF, None, is_dm=True) is True
    assert lpa.phi_allowed(JEFF, "FNDR", is_dm=True) is True


def test_non_custodian_refused_everywhere():
    assert lpa.phi_allowed(RANDO, "LEX") is False
    assert lpa.phi_allowed(RANDO, "LEX-LLC") is False
    assert lpa.phi_allowed(RANDO, None, is_dm=True) is False


# ---------------------------------------------------------------------------
# Fail-closed: missing / empty config
# ---------------------------------------------------------------------------

def test_fail_closed_when_config_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(lpa, "_CUSTODIANS_PATH", tmp_path / "does-not-exist.yaml")
    lpa._cache = frozenset()
    lpa._loaded_at = 0.0
    assert lpa.is_custodian(HARRISON) is False
    assert lpa.phi_allowed(HARRISON, "LEX") is False


def test_fail_closed_when_config_empty(tmp_path, monkeypatch):
    empty = tmp_path / "empty.yaml"
    empty.write_text("custodians: []\n", encoding="utf-8")
    monkeypatch.setattr(lpa, "_CUSTODIANS_PATH", empty)
    lpa._cache = frozenset()
    lpa._loaded_at = 0.0
    assert lpa.is_custodian(HARRISON) is False
    assert lpa.phi_allowed(HARRISON, "LEX") is False


# ---------------------------------------------------------------------------
# check_access integration — phi_custodian relaxes ONLY the phi block
# ---------------------------------------------------------------------------

@pytest.fixture
def _inject_phi_blocked_user(monkeypatch):
    """Inject a synthetic LEX-LLC user with phi + financials blocked."""
    synthetic = {
        JEN: {
            "allowed_entities": ["LEX-LLC", "LEX"],
            "sensitive_topics_blocked": ["phi", "financials"],
        }
    }
    monkeypatch.setattr(user_access, "_permissions_cache", synthetic)
    monkeypatch.setattr(user_access, "_permissions_loaded_at", time.monotonic())
    yield


def test_check_access_phi_blocked_without_custodian(_inject_phi_blocked_user):
    block = user_access.check_access(JEN, "LEX-LLC", "what is the care plan for the client")
    assert block is not None
    assert "EHR" in block


def test_check_access_phi_allowed_with_custodian(_inject_phi_blocked_user):
    block = user_access.check_access(
        JEN, "LEX-LLC", "what is the care plan for the client", phi_custodian=True
    )
    assert block is None


def test_custodian_flag_does_not_unblock_other_topics(_inject_phi_blocked_user):
    # phi_custodian must NOT relax financials (or any non-phi block).
    block = user_access.check_access(
        JEN, "LEX-LLC", "what is the cash flow this week", phi_custodian=True
    )
    assert block is not None
    assert "Financial" in block


def test_custodian_flag_does_not_bypass_entity_auth(_inject_phi_blocked_user):
    # Jen is not authorized for F3E — phi_custodian must not change that.
    block = user_access.check_access(
        JEN, "F3E", "what is the care plan", phi_custodian=True
    )
    assert block is not None


# ---------------------------------------------------------------------------
# Cross-entity guard stays enforced — LEX content blocked in non-LEX channel
# ---------------------------------------------------------------------------

def test_cross_entity_guard_still_blocks_lex_in_non_lex_channel():
    # Independent of the custodian gate: a LEX question in an F3E channel is
    # redirected by the deterministic cross-entity guard before any LLM call.
    redirect = cross_entity_guard.check_cross_entity(
        "what's the lexington ddd revalidation status", "F3E"
    )
    assert redirect is not None
