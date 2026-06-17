"""Tests for the deny-list-aware join pre-pass (Phase 1.4 / F-7).

The join pre-pass must NEVER auto-join a deny-listed channel (personal/family,
LBHS/LTS, NDA) -- that would make Cora a member of a sensitive channel and post a
"Cora joined" message. _should_join routes through slack_sweep_policy.should_ingest.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import bootstrap_channel_membership as boot  # noqa: E402
from cora import slack_sweep_policy  # noqa: E402


def _ch(name, member=False, private=False, cid="C0X"):
    return {"id": cid, "name": name, "is_member": member, "is_private": private}


def test_skips_member():
    assert boot._should_join(_ch("f3e-leadership", member=True)) is False


def test_skips_noise_channels():
    assert boot._should_join(_ch("general")) is False
    assert boot._should_join(_ch("random")) is False


def test_skips_denied(monkeypatch):
    monkeypatch.setattr(
        slack_sweep_policy, "_cache",
        {"deny_by_name": ["personal-tasks"], "deny_by_glob": ["lbhs*", "lts*"]},
    )
    assert boot._should_join(_ch("personal-tasks")) is False
    assert boot._should_join(_ch("lbhs-leadership")) is False
    assert boot._should_join(_ch("lts-finance")) is False


def test_allows_normal_channels(monkeypatch):
    monkeypatch.setattr(slack_sweep_policy, "_cache", {"deny_by_name": ["personal-tasks"]})
    assert boot._should_join(_ch("f3e-leadership")) is True
    assert boot._should_join(_ch("llc-leadership")) is True   # LLC stays (only LBHS/LTS removed)


def test_real_policy_blocks_join_of_sensitive():
    # against the shipped policy, the recurring join must never grab these
    slack_sweep_policy.reset_cache()
    try:
        assert boot._should_join(_ch("personal-tasks", cid="C0B7PMLM9D5")) is False
        assert boot._should_join(_ch("lbhs-copa-diligence", cid="C0B88QJAUMS")) is False
        assert boot._should_join(_ch("lts", cid="C0B6NUHC6HW")) is False
        assert boot._should_join(_ch("f3e-leadership", cid="C0B4KRQT3LY")) is True
    finally:
        slack_sweep_policy.reset_cache()
