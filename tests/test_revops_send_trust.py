"""R2 send-trust ladder loader: tier-2 hard-reject, mailbox universe,
kill-switch precedence (D-051 lens 2)."""

from __future__ import annotations

import pytest

from cora.revops import send_trust


@pytest.fixture(autouse=True)
def _fresh_caches():
    send_trust.clear_caches()
    yield
    send_trust.clear_caches()


def _write_config(monkeypatch, tmp_path, text: str):
    p = tmp_path / "send-trust.yaml"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(send_trust, "_CONFIG_PATH", p)


def test_repo_config_loads_exactly_one_tier1_playbook():
    playbooks = send_trust.load_playbooks(force=True)
    assert set(playbooks) == {"silence_nudge"}
    cfg = playbooks["silence_nudge"]
    assert cfg.tier == 1
    assert cfg.mailbox_allowlist == frozenset({"harrison@hjrglobal.com"})
    assert cfg.approvers == ("U0B2RM2JYJ1",)
    assert cfg.min_silence_days == 7
    assert cfg.max_nudges == 2


def test_tier2_entry_hard_rejected(monkeypatch, tmp_path):
    _write_config(
        monkeypatch,
        tmp_path,
        """
playbooks:
  auto_blast:
    tier: 2
    mailbox_allowlist: [harrison@hjrglobal.com]
    approvers: [U0B2RM2JYJ1]
""",
    )
    assert send_trust.load_playbooks(force=True) == {}
    assert send_trust.effective_tier("auto_blast") == 0


def test_unknown_mailbox_rejected(monkeypatch, tmp_path):
    _write_config(
        monkeypatch,
        tmp_path,
        """
playbooks:
  silence_nudge:
    tier: 1
    mailbox_allowlist: [tommy@f3energy.com]
    approvers: [U0B2RM2JYJ1]
""",
    )
    assert send_trust.load_playbooks(force=True) == {}


def test_tier1_without_approvers_rejected(monkeypatch, tmp_path):
    _write_config(
        monkeypatch,
        tmp_path,
        """
playbooks:
  silence_nudge:
    tier: 1
    mailbox_allowlist: [harrison@hjrglobal.com]
    approvers: []
""",
    )
    assert send_trust.load_playbooks(force=True) == {}


def test_missing_config_means_everything_tier0(monkeypatch, tmp_path):
    monkeypatch.setattr(send_trust, "_CONFIG_PATH", tmp_path / "nope.yaml")
    assert send_trust.load_playbooks(force=True) == {}
    assert send_trust.effective_tier("silence_nudge") == 0


def test_kill_switch_default_off(monkeypatch):
    monkeypatch.delenv("CORA_SEND_LIVE", raising=False)
    assert send_trust.send_live_mode() == "off"
    assert send_trust.effective_tier("silence_nudge") == 0


def test_kill_switch_unrecognized_value_is_off(monkeypatch):
    for val in ("on", "1", "true", "tier2", "TIER1 "):
        monkeypatch.setenv("CORA_SEND_LIVE", val)
        if val.strip().lower() == "tier1":
            continue
        assert send_trust.send_live_mode() == "off", val


def test_effective_tier_requires_env_and_config(monkeypatch):
    monkeypatch.setenv("CORA_SEND_LIVE", "tier1")
    assert send_trust.effective_tier("silence_nudge") == 1
    assert send_trust.effective_tier("unknown_playbook") == 0
    monkeypatch.setenv("CORA_SEND_LIVE", "off")
    assert send_trust.effective_tier("silence_nudge") == 0


def test_is_approver_and_mailbox_allowed():
    assert send_trust.is_approver("silence_nudge", "U0B2RM2JYJ1")
    assert not send_trust.is_approver("silence_nudge", "U0B3RU5Q55G")
    assert not send_trust.is_approver("silence_nudge", "")
    assert send_trust.mailbox_allowed("silence_nudge", "harrison@hjrglobal.com")
    assert send_trust.mailbox_allowed("silence_nudge", "HARRISON@HJRGLOBAL.COM")
    assert not send_trust.mailbox_allowed("silence_nudge", "tommy@f3energy.com")


def test_owner_routing_defaults_to_harrison():
    assert send_trust.owner_for_workstream("Press") == "U0B2RM2JYJ1"
    assert send_trust.owner_for_workstream("Retail") == "U0B3RU5Q55G"
    assert send_trust.owner_for_workstream("NotAWorkstream") == "U0B2RM2JYJ1"
    assert send_trust.owner_for_workstream(None) == "U0B2RM2JYJ1"
