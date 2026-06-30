"""Graduated-trust SHADOW-mode instrumentation (2026-06-29 spec).

The module computes, per knowledge proposal, what graduated trust WOULD have done
and logs it -- ACTING ON NOTHING. These tests pin the three spec-mandated cases
(Tier 0 / Tier 1 / Tier 2-even-if-corroborated), the byte-identical live path, and
the false-positive accounting, plus the category classifier, high-stakes detector,
fail-soft logging, and the --report aggregation.
"""

import json
from types import SimpleNamespace

import pytest

import cora.graduated_trust_shadow as g


# ── Shared fakes: a recognized F3E teammate (Tommy) who is also F3E's owner, and
#    an OSN teammate (Matt) who is NOT F3E's owner. ─────────────────────────────

_ROLES = {
    "U-TOMMY": SimpleNamespace(external=False, all_entities=["F3E"]),
    "U-MATT": SimpleNamespace(external=False, all_entities=["OSN"]),
    "U-HARRISON": SimpleNamespace(external=False,
                                  all_entities=["FNDR", "HJRG", "F3E", "OSN", "LEX"]),
    "U-GUEST": SimpleNamespace(external=True, all_entities=["F3E"]),
}
_OWNERS = {"F3E": "U-TOMMY", "OSN": "U-MATT", "FNDR": "U-HARRISON", "HJRG": "U-HARRISON"}


@pytest.fixture(autouse=True)
def _fake_org(monkeypatch):
    monkeypatch.setattr("cora.org_roles.get_role", lambda sid: _ROLES.get(sid))
    monkeypatch.setattr("cora.gap_autofill.resolve_owner",
                        lambda e: _OWNERS.get((e or "").strip().upper()))
    yield


# ── Category classifier (denylist-first, fail-safe) ────────────────────────────

def test_categorize_allowlist_examples():
    assert g.categorize("the F3E ops dashboard lives in Polar") == "operational"
    assert g.categorize("Tommy now owns the F3E retail relationships") == "ownership"
    assert g.categorize("we have 88 units on hand at the warehouse") == "product_inventory"
    assert g.categorize("the warehouse moved to 500 Brand Blvd") == "addresses"
    assert g.categorize("point of contact for the vendor is jane@acme.com") == "contacts"
    assert g.categorize("shipping to Nimbl is scheduled for Friday") == "logistics"
    assert g.categorize("the process for submitting a PO is step 1 ...") == "sop"


def test_categorize_denylist_examples():
    assert g.categorize("the invoice for $5,000 is due next week") == "money"
    assert g.categorize("the new lease agreement was signed") == "contracts"
    assert g.categorize("the lawsuit settlement with the vendor") == "legal"
    assert g.categorize("the cap table gives Micah 37.5%") == "equity"
    assert g.categorize("his salary is being raised next quarter") == "comp"
    assert g.categorize("our pivot to a subscription-first strategy") == "strategy"


def test_categorize_denylist_wins_over_allowlist():
    # A money/contract keyword present alongside an operational keyword must bin to
    # the denylist (conservative): the near-zero-FP bar on Tier 0 demands it.
    assert g.categorize("use the portal to submit the $5,000 invoice") == "money"
    assert g.categorize("the signed lease is stored in the Drive folder") == "contracts"


def test_categorize_money_spelled_out_and_per_unit(monkeypatch):
    # review LOW: spelled/per-unit/net-terms money cues the noun list missed.
    assert g.categorize("the reorder cost per case went up to 12 dollars") == "money"
    assert g.categorize("the wholesale price list is stored in the Drive folder") == "money"
    assert g.categorize("the new vendor terms are net-60") == "money"


def test_categorize_other_on_miss():
    assert g.categorize("random sentence about the weather today") == "other"
    assert g.categorize("") == "other"


# ── High-stakes detector ───────────────────────────────────────────────────────

def test_high_stakes_lex_entity():
    high, reasons = g.is_high_stakes("clock-in kiosk info", "LEX-LLC", "operational")
    assert high and "lex_entity" in reasons


def test_high_stakes_phi():
    high, reasons = g.is_high_stakes(
        "the client was diagnosed with autism and started risperidone", "F3E", "operational")
    assert high and "phi" in reasons


def test_high_stakes_maricopa():
    high, reasons = g.is_high_stakes("the Maricopa county program", "F3E", "operational")
    assert high and "maricopa" in reasons


def test_high_stakes_denylist_category():
    high, reasons = g.is_high_stakes("the invoice is due", "F3E", "money")
    assert high and any(r.startswith("denylist_category") for r in reasons)


def test_high_stakes_cross_entity():
    high, reasons = g.is_high_stakes("same vendor used", "FNDR", "operational",
                                     entities=["F3E", "OSN"])
    assert high and "cross_entity" in reasons


def test_high_stakes_cross_entity_from_text_when_no_entities_list(monkeypatch):
    # The gap the review caught: known_answer/generic items carry only a singular
    # `entity` (no entities list), so the cross-entity guard must fire from a TEXT
    # keyword scan -- a fact spanning F3E + OSN must be high-stakes even with
    # entities=None and a low-stakes category.
    high, reasons = g.is_high_stakes(
        "we use the same Drive folder for F3 Energy and One Stop Nutrition brand assets",
        "FNDR", "operational", entities=None)
    assert high and "cross_entity_text" in reasons


def test_paired_brand_family_not_cross_entity():
    # F3E + F3C are an intentionally-paired brand family -> NOT cross-entity.
    high, reasons = g.is_high_stakes(
        "F3 Energy and F3 Community share the same brand voice", "F3E", "operational",
        entities=None)
    assert "cross_entity_text" not in reasons


def test_low_stakes_single_entity_operational_is_not_high():
    high, reasons = g.is_high_stakes("the F3E dashboard lives in Polar", "F3E",
                                     "operational", entities=["F3E"])
    assert not high and reasons == []


# ── Contributor recognition / authorized owner ─────────────────────────────────

def test_contributor_recognized_matches_entity():
    assert g.contributor_recognized("U-TOMMY", "F3E") is True
    assert g.contributor_recognized("U-MATT", "F3E") is False      # wrong entity
    assert g.contributor_recognized("U-GUEST", "F3E") is False     # external
    assert g.contributor_recognized("", "F3E") is False            # machine-mined
    assert g.contributor_recognized("U-UNKNOWN", "F3E") is False


def test_authorized_owner_lookup():
    assert g.authorized_owner("U-TOMMY", "F3E") is True
    assert g.authorized_owner("U-MATT", "F3E") is False
    assert g.authorized_owner("", "F3E") is False


# ── Tier classification (the three spec-mandated cases) ────────────────────────

def test_tier0_corroborated_lowstakes_operational_from_teammate():
    tier, decision, _ = g.classify_tier(
        coras_read_verdict="CORROBORATED", category="operational", entity="F3E",
        contributor_id="U-TOMMY", claim_text="the F3E dashboard lives in Polar",
        entities=["F3E"])
    assert tier == g.TIER_0 and decision == g.DECISION_AUTO


def test_tier2_money_even_if_corroborated():
    tier, decision, reasons = g.classify_tier(
        coras_read_verdict="CORROBORATED", category="money", entity="F3E",
        contributor_id="U-TOMMY", claim_text="the invoice for $5,000 is due")
    assert tier == g.TIER_2 and decision == g.DECISION_HARRISON


def test_tier2_lex_even_if_corroborated():
    tier, decision, _ = g.classify_tier(
        coras_read_verdict="CORROBORATED", category="operational", entity="LEX-LLC",
        contributor_id="U-HARRISON", claim_text="the LLC clock-in kiosk")
    assert tier == g.TIER_2


def test_tier2_phi_even_if_corroborated():
    tier, _, _ = g.classify_tier(
        coras_read_verdict="CORROBORATED", category="operational", entity="F3E",
        contributor_id="U-TOMMY",
        claim_text="the client was diagnosed with autism and started risperidone")
    assert tier == g.TIER_2


def test_tier2_cross_entity_even_if_corroborated():
    tier, _, reasons = g.classify_tier(
        coras_read_verdict="CORROBORATED", category="operational", entity="FNDR",
        contributor_id="U-HARRISON", claim_text="same vendor in two places",
        entities=["F3E", "OSN"])
    assert tier == g.TIER_2 and "cross_entity" in reasons


def test_tier2_conflict_even_if_allowlist_and_teammate():
    # A CONFLICTS verdict (contradicts canon) is always Tier 2.
    tier, _, reasons = g.classify_tier(
        coras_read_verdict="CONFLICTS", category="operational", entity="F3E",
        contributor_id="U-TOMMY", claim_text="the dashboard lives in a new place")
    assert tier == g.TIER_2 and "conflicts_canon" in reasons


def test_tier1_uncorroborated_entity_op_from_owner():
    tier, decision, _ = g.classify_tier(
        coras_read_verdict="NET-NEW", category="logistics", entity="F3E",
        contributor_id="U-TOMMY", claim_text="shipping to Nimbl is Friday",
        entities=["F3E"])
    assert tier == g.TIER_1 and decision == g.DECISION_OWNER


def test_tier2_uncorroborated_non_owner_teammate():
    # allowlist + recognized teammate but NOT the authorized owner, and not
    # corroborated -> falls to Harrison (Tier 1 requires the owner).
    tier, _, _ = g.classify_tier(
        coras_read_verdict="NET-NEW", category="operational", entity="OSN",
        contributor_id="U-HARRISON", claim_text="the OSN portal")
    # U-HARRISON recognized for OSN but OSN owner is U-MATT -> not owner -> Tier 2
    assert tier == g.TIER_2


def test_tier2_other_category():
    tier, _, _ = g.classify_tier(
        coras_read_verdict="CORROBORATED", category="other", entity="F3E",
        contributor_id="U-TOMMY", claim_text="random weather sentence")
    assert tier == g.TIER_2


def test_tier2_machine_mined_efficiency_even_if_corroborated():
    # No contributor -> not a recognized teammate -> never Tier 0; not an owner ->
    # never Tier 1. Efficiency findings stay Harrison-gated.
    tier, _, _ = g.classify_tier(
        coras_read_verdict="CORROBORATED", category="operational", entity="F3E",
        contributor_id="", claim_text="automate the weekly export")
    assert tier == g.TIER_2


# ── build_shadow_record ─────────────────────────────────────────────────────────

def _ka(uid="ka1", entity="F3E", answer="the F3E dashboard lives in Polar",
        answered_by="U-TOMMY", verdict="CORROBORATED"):
    return {
        "update_id": uid, "update_type": "known_answer", "confidence": "HIGH",
        "description": f"Knowledge gap fill ({entity}) -- {answer}",
        "payload": {"entity": entity, "question": "where?", "answer": answer,
                    "answered_by": answered_by},
        "_coras_read_verdict": verdict,
    }


def test_build_shadow_record_tier0():
    rec = g.build_shadow_record(_ka(), "CORROBORATED", now_iso="2026-06-30T12:00:00+00:00")
    assert rec["type"] == "shadow_decision"
    assert rec["shadow_tier"] == g.TIER_0
    assert rec["shadow_decision"] == g.DECISION_AUTO
    assert rec["category"] == "operational"
    assert rec["entity"] == "F3E"
    assert rec["contributor"] == "U-TOMMY"
    assert rec["contributor_recognized"] is True
    assert rec["coras_read_verdict"] == "CORROBORATED"
    assert rec["update_id"] == "ka1"


def test_build_shadow_record_unknown_verdict_normalized():
    rec = g.build_shadow_record(_ka(verdict="MAYBE"), "MAYBE")
    assert rec["coras_read_verdict"] == ""          # unknown verdict normalized to ""
    # _ka's contributor (U-TOMMY) is the F3E owner + operational/allowlist, so an
    # uncorroborated item routes to the owner (Tier 1) -- never auto-approves.
    assert rec["shadow_tier"] == g.TIER_1
    assert rec["shadow_decision"] == g.DECISION_OWNER


def test_build_shadow_record_generic_info_for_cora():
    upd = {
        "update_id": "ic1", "update_type": "generic", "confidence": "MED",
        "description": "#info-for-cora from Tommy (F3E): the dashboard lives in Polar",
        "payload": {"text": "the F3E dashboard lives in Polar", "entity": "F3E",
                    "author_id": "U-TOMMY", "source": "info-for-cora"},
    }
    rec = g.build_shadow_record(upd, "CORROBORATED")
    assert rec["contributor"] == "U-TOMMY"
    assert rec["shadow_tier"] == g.TIER_0


def test_build_shadow_record_preview_redacts_phi():
    upd = {
        "update_id": "p1", "update_type": "generic", "confidence": "MED",
        "description": "the client was diagnosed with autism and prescribed risperidone",
        "payload": {"text": "x", "entity": "LEX-LLC", "author_id": "U-HARRISON",
                    "source": "info-for-cora"},
    }
    rec = g.build_shadow_record(upd, "")
    assert rec["preview"] == "[redacted]"


# ── Logging: write, kill switch, fail-soft ─────────────────────────────────────

def test_record_shadow_decisions_writes(tmp_path):
    items = [_ka("a"), _ka("b", entity="F3E", answer="the invoice for $5,000 is due")]
    n = g.record_shadow_decisions(items, log_dir=tmp_path)
    assert n == 2
    files = list(tmp_path.glob("graduated-trust-shadow-*.jsonl"))
    assert len(files) == 1
    recs = [json.loads(l) for l in files[0].read_text(encoding="utf-8").splitlines()]
    assert {r["update_id"] for r in recs} == {"a", "b"}


def test_kill_switch_disables_logging(tmp_path, monkeypatch):
    monkeypatch.setenv("CORA_GRADUATED_SHADOW", "0")
    n = g.record_shadow_decisions([_ka("a")], log_dir=tmp_path)
    assert n == 0
    assert not list(tmp_path.glob("graduated-trust-shadow-*.jsonl"))


def test_record_shadow_decisions_failsoft_on_bad_item(tmp_path):
    # A malformed item must not raise; good items still write.
    bad = "not-a-dict"
    items = [_ka("good"), bad]  # type: ignore[list-item]
    n = g.record_shadow_decisions(items, log_dir=tmp_path)  # must not raise
    assert n == 1  # the good one wrote; the bad one was skipped


def test_record_shadow_reactions_only_approved_dismissed(tmp_path):
    pairs = [
        ({"update_id": "a"}, {"action": "APPROVED", "reaction": "+1"}),
        ({"update_id": "b"}, {"action": "DISMISSED", "reaction": "-1"}),
        ({"update_id": "c"}, {"action": "COMMENT_REQUESTED", "reaction": "eyes"}),
        ({"update_id": "d"}, {"action": "OTHER"}),
    ]
    n = g.record_shadow_reactions(pairs, log_dir=tmp_path)
    assert n == 2
    recs = [json.loads(l) for f in tmp_path.glob("*.jsonl")
            for l in f.read_text(encoding="utf-8").splitlines()]
    actions = {r["update_id"]: r["reaction_action"] for r in recs}
    assert actions == {"a": "APPROVED", "b": "DISMISSED"}


# ── --report aggregation + false-positive accounting ───────────────────────────

def test_report_counts_and_false_positive_accounting(tmp_path):
    # One Tier-0 item (a), one Tier-2 money item (b), one Tier-1 owner item (c).
    items = [
        _ka("a", answer="the F3E dashboard lives in Polar", verdict="CORROBORATED"),  # T0
        _ka("b", answer="the invoice for $5,000 is due", verdict="CORROBORATED"),     # T2 money
        _ka("c", answer="shipping to Nimbl is on Friday", verdict="NET-NEW"),         # T1 owner
    ]
    for it in items:
        it["_coras_read_verdict"] = it["payload"].get("_v") or it["_coras_read_verdict"]
    g.record_shadow_decisions(items, log_dir=tmp_path)

    # Harrison thumbs-DOWN the Tier-0 item -> a false positive.
    g.record_shadow_reactions([({"update_id": "a"}, {"action": "DISMISSED"})],
                              log_dir=tmp_path)

    stats = g.build_report(tmp_path)
    assert stats["total_decisions"] == 3
    assert stats["by_tier"].get("0") == 1
    assert stats["by_tier"].get("1") == 1
    assert stats["by_tier"].get("2") == 1
    assert stats["would_tier0"] == 1
    assert stats["would_tier0_reacted"] == 1
    assert stats["would_tier0_false_positives"] == 1
    assert stats["would_tier0_false_positive_rate"] == 1.0
    assert stats["would_tier0_false_positive_ids"] == ["a"]


def test_report_true_positive_not_counted_as_fp(tmp_path):
    g.record_shadow_decisions([_ka("a", answer="the F3E dashboard lives in Polar")],
                              log_dir=tmp_path)
    g.record_shadow_reactions([({"update_id": "a"}, {"action": "APPROVED"})],
                              log_dir=tmp_path)
    stats = g.build_report(tmp_path)
    assert stats["would_tier0_true_positives"] == 1
    assert stats["would_tier0_false_positives"] == 0
    assert stats["would_tier0_false_positive_rate"] == 0.0


def test_report_pending_tier0_not_in_fp_denominator(tmp_path):
    g.record_shadow_decisions([_ka("a", answer="the F3E dashboard lives in Polar")],
                              log_dir=tmp_path)
    stats = g.build_report(tmp_path)   # no reaction yet
    assert stats["would_tier0"] == 1
    assert stats["would_tier0_pending"] == 1
    assert stats["would_tier0_reacted"] == 0
    assert stats["would_tier0_false_positive_rate"] == 0.0   # denom guard


def test_report_format_is_ascii(tmp_path):
    g.record_shadow_decisions([_ka("a")], log_dir=tmp_path)
    text = g.format_report(g.build_report(tmp_path))
    text.encode("ascii")  # raises if any non-ASCII leaks (cp1252 stdout safety)


def test_report_empty_when_no_logs(tmp_path):
    stats = g.build_report(tmp_path)
    assert stats["total_decisions"] == 0
    assert stats["would_tier0"] == 0
    # format must not crash on empty
    g.format_report(stats)


def test_report_tolerates_non_dict_json_lines(tmp_path):
    # review MEDIUM: a valid-JSON-but-non-dict line (number/string/null/array) must
    # NOT crash build_report on rec.get(); it is skipped, good records still count.
    g.record_shadow_decisions([_ka("a", answer="the F3E dashboard lives in Polar")],
                              log_dir=tmp_path)
    f = sorted(tmp_path.glob("graduated-trust-shadow-*.jsonl"))[0]
    with f.open("a", encoding="utf-8") as fh:
        fh.write("42\n")
        fh.write('"a bare string"\n')
        fh.write("null\n")
        fh.write("[1, 2, 3]\n")
    stats = g.build_report(tmp_path)  # must not raise
    assert stats["total_decisions"] == 1  # only the real record counted


def test_report_per_week_provisional_under_one_week(tmp_path):
    g.record_shadow_decisions([_ka("a", answer="the F3E dashboard lives in Polar")],
                              log_dir=tmp_path)
    stats = g.build_report(tmp_path)
    assert stats["would_tier0_rate_provisional"] is True
    assert "PROVISIONAL" in g.format_report(stats)
