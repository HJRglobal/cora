"""Tests for the outbound channel-scope content guard (F-08 family, 2026-07-12).

Covers each confidential class refusing in a wrong channel AND -- co-equally --
NOT over-refusing the legitimate answers the mega-smoke graded as PASS (a product
price, a withheld deal valuation, founder-ops portfolio financials, permitted
creator content). Channel scoping for the dashboard-backed classes is delegated to
dashboard_access + the real data/maps/dashboard-access.yaml, so these are true
end-to-end guard decisions.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cora import channel_content_guard as ccg  # noqa: E402
from cora import dashboard_access  # noqa: E402

HARRISON = "U0B2RM2JYJ1"   # dm_user in dashboard-access.yaml
OTHER = "U0TEAMMATE999"


def _g(text, *, entity, tier, channel, user=HARRISON, is_dm=False):
    dashboard_access.invalidate_cache()
    return ccg.guard_outbound(
        text, entity=entity, tier=tier, channel_name=channel,
        user_id=user, is_dm=is_dm,
    )


# ── personal insurance (OneAmerica) — DM-to-Harrison only ────────────────────
_ONEAMERICA = ("Your OneAmerica whole life cash value is $3,806,354 with "
               "$3,531,409 in policy loans (93% LTV).")


def test_oneamerica_refused_in_channel():
    out, cls = _g(_ONEAMERICA, entity="FNDR", tier="TIER_1", channel="cora-build")
    assert cls == "personal_insurance"
    assert "3,806,354" not in out and "OneAmerica" not in out


def test_oneamerica_allowed_in_harrison_dm():
    out, cls = _g(_ONEAMERICA, entity="FNDR", tier="TIER_3", channel="dm", is_dm=True)
    assert cls is None
    assert out == _ONEAMERICA


def test_oneamerica_refused_in_other_user_dm():
    out, cls = _g(_ONEAMERICA, entity="FNDR", tier="TIER_3", channel="dm",
                  user=OTHER, is_dm=True)
    assert cls == "personal_insurance"


# ── capital program — DM-to-Harrison only ────────────────────────────────────
def test_capital_program_seat_structure_refused_in_channel():
    text = ("The raise is a $25M valuation with $500K seats at 2.0% equity for "
            "ambassadors.")
    out, cls = _g(text, entity="FNDR", tier="TIER_1", channel="cora-build")
    assert cls == "capital_program"
    assert "$25M" not in out and "$500K" not in out


def test_capital_program_literal_phrase_refused():
    out, cls = _g("The capital program terms are still confidential.",
                  entity="FNDR", tier="TIER_1", channel="cora-build")
    assert cls == "capital_program"


def test_capital_program_allowed_in_harrison_dm():
    text = "The capital program: $25M valuation, $500K seats at 2.0% equity."
    out, cls = _g(text, entity="FNDR", tier="TIER_3", channel="dm", is_dm=True)
    assert cls is None


def test_watchtower_valuation_withheld_answer_not_over_refused():
    # S2.8 PASS: an answer that WITHHOLDS a deal valuation (no figure, no seat)
    # must not trip capital_program or company_financials.
    text = ("I can't share the Watchtower valuation or investment size -- those "
            "live in the agreement.")
    out, cls = _g(text, entity="HJRPROD", tier="TIER_1", channel="hjrprod-leadership")
    assert cls is None
    assert out == text


# ── travel points — DM-to-Harrison only ──────────────────────────────────────
def test_travel_points_refused_in_channel():
    text = ("Your Companion Pass progress is ~114,698 toward 135,000; A-List "
            "Preferred is active.")
    out, cls = _g(text, entity="FNDR", tier="TIER_1", channel="cora-build")
    assert cls == "travel_points"


def test_travel_points_allowed_in_harrison_dm():
    out, cls = _g("Companion Pass is close.", entity="FNDR", tier="TIER_3",
                  channel="dm", is_dm=True)
    assert cls is None


# ── F3E creator CRM — F3E channels + founder only ─────────────────────────────
_CREATOR = ("The F3E creator CRM roster has 74 people (base appwF6W6eVTvPFjct); "
            "hand it to Alex.")


def test_creator_crm_refused_in_osn_channel():
    # F-13: creator CRM dumped into #osn-leadership.
    out, cls = _g(_CREATOR, entity="OSN", tier="TIER_1", channel="osn-leadership")
    assert cls == "creator_crm"
    assert "appwF6W6eVTvPFjct" not in out


def test_creator_crm_allowed_in_f3_athletes():
    out, cls = _g(_CREATOR, entity="F3E", tier="TIER_3", channel="f3-athletes")
    assert cls is None


def test_creator_crm_allowed_in_f3e_leadership():
    out, cls = _g(_CREATOR, entity="F3E", tier="TIER_1", channel="f3e-leadership")
    assert cls is None


# ── founder content pipeline — founder-operations only ────────────────────────
def test_content_pipeline_refused_in_f3_athletes():
    text = "The content pipeline has 3 overdue freelancer deliverables this week."
    out, cls = _g(text, entity="F3E", tier="TIER_3", channel="f3-athletes")
    assert cls == "content_pipeline"


def test_content_pipeline_allowed_in_founder_operations():
    text = "The content pipeline has 3 overdue freelancer deliverables this week."
    out, cls = _g(text, entity="FNDR", tier="TIER_3", channel="founder-operations")
    assert cls is None


# ── company financials — TIER_1 / founder / DM only ──────────────────────────
_REVENUE = "In May 2026, F3 did $320,615 in gross revenue."


def test_company_financials_refused_in_tier3_entity_channel():
    # F-12: revenue leaked into TIER_3 #f3-athletes.
    out, cls = _g(_REVENUE, entity="F3E", tier="TIER_3", channel="f3-athletes")
    assert cls == "company_financials"
    assert "320,615" not in out


def test_company_financials_allowed_in_tier1_channel():
    out, cls = _g(_REVENUE, entity="F3E", tier="TIER_1", channel="f3e-leadership")
    assert cls is None
    assert out == _REVENUE


def test_company_financials_allowed_in_founder_operations():
    # S2.10 non-regression: founder-operations is TIER_3 by the function namer but
    # a legitimate portfolio-oversight surface (entity FNDR).
    out, cls = _g(_REVENUE, entity="FNDR", tier="TIER_3", channel="founder-operations")
    assert cls is None


def test_company_financials_allowed_in_dm():
    out, cls = _g(_REVENUE, entity="FNDR", tier="TIER_3", channel="dm", is_dm=True)
    assert cls is None


def test_product_price_not_over_refused():
    # S2.1 PASS: a small product price with no company-finance term.
    text = "Pure Original is $36.99 per 12-pack."
    out, cls = _g(text, entity="F3E", tier="TIER_3", channel="f3-athletes")
    assert cls is None
    assert out == text


def test_revenue_word_without_figure_not_refused():
    out, cls = _g("We want to grow revenue next year.", entity="F3E",
                  tier="TIER_3", channel="f3-athletes")
    assert cls is None


# ── clean text + edge cases ───────────────────────────────────────────────────
def test_clean_operational_answer_untouched_everywhere():
    text = "The Tucson stove vendor is Apex Appliance."
    for ch, ent, tier in [("f3-athletes", "F3E", "TIER_3"),
                          ("osn-leadership", "OSN", "TIER_1"),
                          ("cora-build", "FNDR", "TIER_1")]:
        out, cls = _g(text, entity=ent, tier=tier, channel=ch)
        assert cls is None and out == text


def test_empty_and_none_passthrough():
    assert ccg.guard_outbound("", entity="F3E", tier="TIER_3",
                              channel_name="f3-athletes", user_id=HARRISON,
                              is_dm=False) == ("", None)
    assert ccg.guard_outbound(None, entity="F3E", tier="TIER_3",
                             channel_name="f3-athletes", user_id=HARRISON,
                             is_dm=False) == (None, None)


def test_most_confidential_class_wins_tie():
    # OneAmerica + a revenue figure in a TIER_3 channel -> personal_insurance (the
    # more-confidential class) wins, not company_financials.
    text = _ONEAMERICA + " Also F3 did $320,615 in gross revenue."
    out, cls = _g(text, entity="F3E", tier="TIER_3", channel="f3-athletes")
    assert cls == "personal_insurance"


# ── wiring: the guard is applied at every _dispatch_qa serve site ─────────────
def test_guard_wired_into_dispatch_qa():
    app_src = (Path(__file__).resolve().parent.parent / "src" / "cora" / "app.py").read_text(
        encoding="utf-8"
    )
    assert "import channel_content_guard" in app_src
    assert "channel_content_guard.guard_outbound" in app_src
    # helper def + cache-hit + non-stream + stream-frame + stream-final = 5 refs.
    assert app_src.count("_guard_content(") >= 5
