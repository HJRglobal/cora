"""Tests for the graduated-trust auto-write flip (Slice F, §7B).

Security focus (§7D):
  1. NO high-stakes / conflicts-with-canon / Tier-2 item can ever auto-write.
  2. every auto-write is BOTH audited AND revertible.
  3. the flip is default-OFF (CORA_AUTOWRITE_LIVE unset).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cora import knowledge_review as kr


def _rk():
    sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
    import run_knowledge_review as m  # noqa: E402
    return m


RK = _rk()
HARRISON = kr.HARRISON_SLACK_USER_ID


# ── level gate (default OFF) ──────────────────────────────────────────────────
def test_autowrite_level_default_off(monkeypatch):
    monkeypatch.delenv("CORA_AUTOWRITE_LIVE", raising=False)
    assert kr.autowrite_level() == "off"


@pytest.mark.parametrize("val,exp", [
    ("tier0", "tier0"), ("all", "all"), ("TIER0", "tier0"),
    ("off", "off"), ("garbage", "off"), ("", "off"),
])
def test_autowrite_level_parse(monkeypatch, val, exp):
    monkeypatch.setenv("CORA_AUTOWRITE_LIVE", val)
    assert kr.autowrite_level() == exp


# ── eligibility gate: Tier-2 / high-stakes / conflict NEVER auto-write ────────
def _patch_classifier(monkeypatch, *, tier, high=False, conflicts=False):
    monkeypatch.setattr(RK.gts, "build_shadow_record",
                        lambda u, v: {"shadow_tier": tier, "entity": "F3E",
                                      "category": "operational", "entities": [],
                                      "conflicts": conflicts})
    monkeypatch.setattr(RK.gts, "is_high_stakes", lambda *a, **k: (high, []))
    monkeypatch.setattr(RK.gts, "claim_text", lambda u: "some claim")


def _u():
    return {"update_id": "u1", "update_type": "known_answer",
            "payload": {"entity": "F3E"}, "_coras_read_verdict": "CORROBORATED"}


def test_tier2_never_eligible(monkeypatch):
    _patch_classifier(monkeypatch, tier=2)
    for level in ("tier0", "all"):
        elig, tier, why = RK._autowrite_eligible(_u(), level)
        assert elig is False and tier == 2


def test_tier0_eligible_at_tier0_and_all(monkeypatch):
    _patch_classifier(monkeypatch, tier=0)
    assert RK._autowrite_eligible(_u(), "tier0")[0] is True
    assert RK._autowrite_eligible(_u(), "all")[0] is True
    assert RK._autowrite_eligible(_u(), "off")[0] is False  # off never eligible


def test_tier1_eligible_only_at_all(monkeypatch):
    _patch_classifier(monkeypatch, tier=1)
    assert RK._autowrite_eligible(_u(), "tier0")[0] is False   # tier1 needs 'all'
    assert RK._autowrite_eligible(_u(), "all")[0] is True


def test_high_stakes_belt_blocks_even_tier0(monkeypatch):
    _patch_classifier(monkeypatch, tier=0, high=True)
    elig, tier, why = RK._autowrite_eligible(_u(), "all")
    assert elig is False and why == "high_stakes_or_conflict"


def test_conflicts_blocks_even_tier0(monkeypatch):
    _patch_classifier(monkeypatch, tier=0, conflicts=True)
    assert RK._autowrite_eligible(_u(), "all")[0] is False


def test_belt_fails_closed_on_exception(monkeypatch):
    monkeypatch.setattr(RK.gts, "build_shadow_record",
                        lambda u, v: {"shadow_tier": 0, "entity": "F3E", "category": "operational",
                                      "entities": [], "conflicts": False})
    def boom(*a, **k):
        raise RuntimeError("phi_guard import broke")
    monkeypatch.setattr(RK.gts, "is_high_stakes", boom)
    monkeypatch.setattr(RK.gts, "claim_text", lambda u: "x")
    elig, _t, why = RK._autowrite_eligible(_u(), "all")
    assert elig is False and why == "high_stakes_or_conflict"   # fail-closed


# ── apply_autowrite: applies + audits + captures revert payload ───────────────
@pytest.fixture
def kadir(tmp_path, monkeypatch):
    """A temp known-answers-style target + temp audit path + stubbed resolve."""
    target = tmp_path / "f3e.md"
    target.write_text("# F3E known answers\n\n## Known facts\n\n", encoding="utf-8")
    audit = tmp_path / "cora-autowrite-audit.jsonl"
    monkeypatch.setattr(kr, "_AUTOWRITE_AUDIT_PATH", audit)
    monkeypatch.setattr(kr, "_autowrite_target_files", lambda: [target])
    resolved = []
    monkeypatch.setattr(kr, "resolve_update", lambda uid, state, reason="": resolved.append((uid, state, reason)) or True)
    return target, audit, resolved


def test_apply_autowrite_writes_audit_and_revert_payload(kadir, monkeypatch):
    target, audit, resolved = kadir

    def fake_apply(update):
        # simulate an appender inserting a block into the target
        txt = target.read_text(encoding="utf-8")
        target.write_text(txt + "**Q:** hi?\n**A:** yes.\n", encoding="utf-8")
        return True, "wrote 1 fact"
    monkeypatch.setattr(kr, "apply_knowledge_update", fake_apply)

    ok, summary = kr.apply_autowrite(_u(), tier=0, reason="auto_tier0", contributor="U1")
    assert ok is True
    assert resolved == [("u1", "APPROVED", "auto_tier0")]
    recs = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(recs) == 1
    r = recs[0]
    assert r["tier"] == 0 and r["update_id"] == "u1" and r["reverted"] is False
    assert r["revert"]["target_file"] == str(target)
    assert "**A:** yes." in r["revert"]["added_lines"]


def test_apply_autowrite_apply_failure_no_audit_no_resolve(kadir, monkeypatch):
    target, audit, resolved = kadir
    monkeypatch.setattr(kr, "apply_knowledge_update", lambda u: (False, "looks like PHI"))
    ok, summary = kr.apply_autowrite(_u(), tier=0, reason="auto_tier0")
    assert ok is False and "PHI" in summary
    assert resolved == []                 # not resolved
    assert not audit.exists() or audit.read_text(encoding="utf-8").strip() == ""


# ── revert round-trip ─────────────────────────────────────────────────────────
def test_process_autowrite_revert(tmp_path, monkeypatch):
    target = tmp_path / "f3e.md"
    target.write_text("# head\n\n## Known facts\n\n**Q:** hi?\n**A:** yes.\n\nkeep me\n", encoding="utf-8")
    audit = tmp_path / "audit.jsonl"
    audit.write_text(json.dumps({
        "ts": "2026-07-21T00:00:00+00:00", "update_id": "u1", "update_type": "known_answer",
        "decision_reason": "auto_tier0", "reverted": False,
        "revert": {"target_file": str(target), "added_lines": ["**Q:** hi?", "**A:** yes."]},
    }) + "\n", encoding="utf-8")
    monkeypatch.setattr(kr, "_AUTOWRITE_AUDIT_PATH", audit)
    monkeypatch.setattr(kr, "resolve_update", lambda *a, **k: True)

    # non-Harrison refused
    assert kr.process_autowrite_revert("u1", "U_OTHER")[0] == "not_authorized"

    outcome, msg = kr.process_autowrite_revert("u1", HARRISON)
    assert outcome == "reverted"
    body = target.read_text(encoding="utf-8")
    assert "**A:** yes." not in body and "keep me" in body   # block removed, rest intact

    # second revert is a no-op
    assert kr.process_autowrite_revert("u1", HARRISON)[0] == "already_reverted"
    # a revert marker was appended
    recs = [json.loads(l) for l in audit.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert any(r.get("decision_reason") == "revert" for r in recs)


def test_process_autowrite_revert_not_found(tmp_path, monkeypatch):
    audit = tmp_path / "audit.jsonl"
    audit.write_text("", encoding="utf-8")
    monkeypatch.setattr(kr, "_AUTOWRITE_AUDIT_PATH", audit)
    assert kr.process_autowrite_revert("nope", HARRISON)[0] == "not_found"


# ── digest blocks + WoW counting ──────────────────────────────────────────────
def test_build_digest_blocks_have_revert_buttons():
    recs = [{"update_id": "a", "update_type": "known_answer", "tier": 0, "entity": "F3E", "summary": "x"}]
    fallback, blocks = kr.build_autowrite_digest_blocks(recs)
    btns = [e for b in blocks if b.get("type") == "actions" for e in b["elements"]]
    assert btns and btns[0]["action_id"] == kr.ACTION_AUTOWRITE_REVERT and btns[0]["value"] == "a"


def test_digest_wow_counts(monkeypatch):
    import run_autowrite_digest as D
    now = 1_800_000_000.0
    recs = [
        {"ts": _iso(now - 1 * 86400), "update_id": "a", "decision_reason": "auto_tier0", "reverted": False},
        {"ts": _iso(now - 2 * 86400), "update_id": "b", "decision_reason": "auto_tier1", "reverted": False},
        {"ts": _iso(now - 9 * 86400), "update_id": "c", "decision_reason": "auto_tier0", "reverted": False},  # prev week
        {"ts": _iso(now - 1 * 86400), "update_id": "d", "decision_reason": "revert"},                          # revert this week
        {"ts": _iso(now - 1 * 86400), "update_id": "b", "decision_reason": "revert"},                          # b reverted -> excluded
    ]
    monkeypatch.setattr(kr, "read_autowrite_audit", lambda since_ts=None: recs)
    stats, items = D.build_digest(now, days=7)
    assert stats["this_week"] == 1 and stats["prev_week"] == 1 and stats["reverts_this_week"] == 2
    assert {i["update_id"] for i in items} == {"a"}   # b excluded (reverted), c prev-week


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
