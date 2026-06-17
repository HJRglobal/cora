"""Tests for slack_sweep_policy (Phase 1.4, audit F-1/F-2 + gate G-A).

Logic tests inject a policy dict directly; the last test asserts the shipped
data/maps/slack-sweep-policy.yaml denies the real sensitive channels and leaves
normal leadership channels ingestible (the deny-list model, not deny-all-private).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import slack_sweep_policy as policy  # noqa: E402


@pytest.fixture
def fake_policy(monkeypatch):
    def _set(p: dict):
        monkeypatch.setattr(policy, "_cache", p)
    return _set


# ── deny-list logic (injected policy) ────────────────────────────────────────

def test_deny_by_exact_name(fake_policy):
    fake_policy({"deny_by_name": ["personal-tasks"]})
    assert policy.is_denied("personal-tasks") is True
    assert policy.should_ingest("personal-tasks") is False
    assert policy.should_ingest("f3e-leadership") is True


def test_deny_by_name_case_insensitive(fake_policy):
    fake_policy({"deny_by_name": ["personal-tasks"]})
    assert policy.is_denied("Personal-Tasks") is True


def test_deny_by_glob_lbhs_lts(fake_policy):
    fake_policy({"deny_by_glob": ["lbhs*", "lts*"]})
    assert policy.is_denied("lbhs-leadership") is True
    assert policy.is_denied("lts-finance") is True
    assert policy.is_denied("lbhs") is True
    # LLC/LLA stay (G-A.2 removes only LBHS/LTS); normal channels untouched.
    assert policy.is_denied("llc-leadership") is False
    assert policy.is_denied("lla-maryvale") is False
    assert policy.is_denied("f3e-leadership") is False


def test_deny_by_id(fake_policy):
    fake_policy({"deny_by_id": ["C0B2NMLK7CK", "C0B76Q7QX09"]})
    assert policy.is_denied("anything", "C0B2NMLK7CK") is True
    assert policy.is_denied("cora-kq-lex-lbhs", "C0B76Q7QX09") is True
    assert policy.is_denied("normal", "C0BNORMAL1") is False


def test_private_not_denied_still_ingests_without_allowlist(fake_policy):
    # deny-list model: a private channel that isn't denied + no allowlist -> ingest
    fake_policy({"deny_by_name": ["personal-tasks"]})
    assert policy.should_ingest("hjrg-leadership", "C0B1", is_private=True) is True


def test_private_allowlist_gates_only_private(fake_policy):
    fake_policy({"private_allowlist": ["hjrg-leadership"]})
    assert policy.should_ingest("hjrg-leadership", "C0B1", is_private=True) is True
    assert policy.should_ingest("secret-private", "C0B2", is_private=True) is False
    # allow-list gates private only; a public channel still ingests
    assert policy.should_ingest("public-chan", "C0B3", is_private=False) is True


def test_empty_policy_allows_all(fake_policy):
    fake_policy({})
    assert policy.should_ingest("anything", "C0B1") is True


# ── shipped policy content ───────────────────────────────────────────────────

def test_real_policy_denies_sensitive_channels():
    policy.reset_cache()
    try:
        for name in ["personal-tasks", "kids-schedules-and-tasks",
                     "alison-s-previously-assigned-tasks", "lbhs-leadership",
                     "lts-finance", "lbhs-copa-diligence", "general-do-not-use"]:
            assert policy.is_denied(name), f"{name} should be denied by the shipped policy"
        # private cora-kq channels (prefix glob misses cora-kq-* names) -> by id
        assert policy.is_denied("cora-kq-lex-lbhs", "C0B76Q7QX09")
        assert policy.is_denied("cora-kq-lex-lts", "C0B6YJ1MQFM")
        # normal channels MUST stay ingestible (no regression)
        assert not policy.is_denied("f3e-leadership")
        assert not policy.is_denied("llc-leadership")
        assert not policy.is_denied("hjrg-finance")
    finally:
        policy.reset_cache()
