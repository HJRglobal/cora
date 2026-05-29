"""
FNDR / HJRG system prompt content tests.

Verifies that design/system-prompts/fndr.md contains all locked guardrails,
portfolio context, and behavioral rules. These tests are documentation-as-tests:
if a future edit accidentally drops a locked decision, a test fails.

Parallel-chat naming convention: test_fndr_*.py (per HJRG wishlist Category 4
parallel-chat rules — HJRG-scoped tests live here).

All tests are pure-Python string assertions against the prompt file — no
network calls, no imports from src/. Restart Cora after any fndr.md edit.

Categories covered:
  Cat 2 #1  — Portfolio operating context (locked)
  Cat 2 #2  — Visibility CPA team exclusion
  Cat 2 #4  — Harrison sole-authority doctrine
  Cat 5 #1  — Visibility CPA exclusion enforcement
  Cat 5 #2  — Sole-authority enforcement
  Cat 5 #5  — Cross-entity confidentiality
  Voice     — Communication preference lock (2026-05-23)
  Regression — Financial guardrail, source-opacity, ads tools, knowledge gap
"""

import pathlib

PROMPT_PATH = pathlib.Path(__file__).parent.parent / "design" / "system-prompts" / "fndr.md"
PROMPT = PROMPT_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Voice & style — communication preference lock (2026-05-23)
# ---------------------------------------------------------------------------

class TestCommunicationPreferenceLock:
    """Never encourage breaks, sleep, or pauses. Locked 2026-05-23."""

    def test_no_break_encouragement_rule_present(self):
        assert "break" in PROMPT.lower(), \
            "Communication preference lock must mention 'break' as a prohibited encouragement"

    def test_sleep_prohibition_present(self):
        assert "sleep" in PROMPT.lower(), \
            "Communication preference lock must mention 'sleep' as a prohibited encouragement"

    def test_pause_prohibition_present(self):
        assert "pause" in PROMPT.lower(), \
            "Communication preference lock must mention 'pause' as a prohibited encouragement"

    def test_harrison_sets_cadence_framing(self):
        assert "Harrison sets the cadence" in PROMPT or "he sets the cadence" in PROMPT.lower(), \
            "Prompt must state Harrison sets the cadence explicitly"

    def test_lock_date_referenced(self):
        assert "2026-05-23" in PROMPT, \
            "Communication preference lock date (2026-05-23) must appear in prompt"

    def test_no_call_it_a_night_prohibited(self):
        assert "call it a night" in PROMPT, \
            "Prompt must explicitly prohibit 'call it a night' framing"


# ---------------------------------------------------------------------------
# Category 2 #2 / Category 5 #1 — Visibility CPA team exclusion
# ---------------------------------------------------------------------------

class TestVisibilityCPAExclusion:
    """Visibility CPA team must never be added as Slack draft recipients."""

    def test_visibility_cpa_section_present(self):
        assert "Visibility CPA team" in PROMPT, \
            "Visibility CPA team exclusion section must be present in prompt"

    def test_andrew_stubbs_listed(self):
        assert "Andrew Stubbs" in PROMPT, \
            "Andrew Stubbs must be listed in Visibility CPA exclusion"

    def test_andrew_stubbs_email_listed(self):
        assert "astubbs@visibilitycpa.com" in PROMPT, \
            "Andrew Stubbs email must be in prompt so Cora can refuse on exact match"

    def test_sarah_bertoglio_listed(self):
        assert "Sarah Bertoglio" in PROMPT, \
            "Sarah Bertoglio must be listed in Visibility CPA exclusion"

    def test_sarah_bertoglio_email_listed(self):
        assert "estubbs@visibilitycpa.com" in PROMPT, \
            "Sarah Bertoglio email must be in prompt"

    def test_hayden_greber_listed(self):
        assert "Hayden Greber" in PROMPT, \
            "Hayden Greber must be listed in Visibility CPA exclusion"

    def test_hayden_greber_email_listed(self):
        assert "hayden@visibilitycpa.com" in PROMPT, \
            "Hayden Greber email must be in prompt"

    def test_emily_stubbs_listed(self):
        assert "Emily Stubbs" in PROMPT, \
            "Emily Stubbs must be listed in Visibility CPA exclusion"

    def test_michael_dibenedetto_listed(self):
        assert "Michael DiBenedetto" in PROMPT, \
            "Michael DiBenedetto must be listed in Visibility CPA exclusion"

    def test_andrew_lee_listed(self):
        assert "Andrew Lee" in PROMPT, \
            "Andrew Lee must be listed in Visibility CPA exclusion"

    def test_never_add_as_recipient(self):
        assert "NEVER" in PROMPT, \
            "Prompt must use 'NEVER' to make the recipient exclusion explicit and strong"

    def test_hjrg_finance_channel_is_the_target(self):
        assert "#hjrg-finance" in PROMPT, \
            "Finance summaries must be directed to #hjrg-finance, not to Visibility CPA via email"

    def test_not_in_slack_workspace_stated(self):
        assert "NOT in the HJR Slack workspace" in PROMPT or "not in the HJR Slack" in PROMPT.lower(), \
            "Prompt must state that Visibility CPA team is not in the Slack workspace"

    def test_gmail_draft_tool_named_in_exclusion(self):
        assert "gmail_create_draft" in PROMPT, \
            "The gmail_create_draft tool must be explicitly named in the exclusion rule"

    def test_what_to_do_instead_guidance(self):
        # Cora should have an alternative action: help draft for Harrison to send manually
        assert "manually" in PROMPT, \
            "Prompt must guide Cora toward helping draft the body for Harrison to send manually instead"


# ---------------------------------------------------------------------------
# Category 2 #4 / Category 5 #2 — Harrison sole-authority doctrine
# ---------------------------------------------------------------------------

class TestSoleAuthorityDoctrine:
    """Harrison is sole authority on access / money / contracts / comms."""

    def test_sole_authority_section_present(self):
        assert "sole authority" in PROMPT.lower() or "sole-authority" in PROMPT.lower(), \
            "Harrison sole-authority doctrine section must be present in prompt"

    def test_sole_authority_lock_date(self):
        assert "2026-05-21" in PROMPT, \
            "Sole-authority doctrine lock date (2026-05-21) must be referenced in prompt"

    def test_shaun_listed_as_operator(self):
        assert "Shaun Hawkins" in PROMPT, \
            "Shaun Hawkins must be named as an operator (not an approval gate)"

    def test_hannah_listed_as_operator(self):
        assert "Hannah Grant" in PROMPT, \
            "Hannah Grant must be named as an operator (not an approval gate)"

    def test_matt_listed_as_operator(self):
        assert "Matt Petrovich" in PROMPT, \
            "Matt Petrovich must be named as an operator (not an approval gate)"

    def test_justin_listed_as_operator(self):
        assert "Justin Moran" in PROMPT, \
            "Justin Moran must be named as an operator (not an approval gate)"

    def test_larry_listed_as_operator(self):
        assert "Larry Stone" in PROMPT, \
            "Larry Stone must be named as an operator (not an approval gate)"

    def test_alex_listed_as_operator(self):
        assert "Alex Cordova" in PROMPT, \
            "Alex Cordova must be named as an operator (not an approval gate)"

    def test_tommy_listed_as_operator(self):
        assert "Tommy Anderson" in PROMPT, \
            "Tommy Anderson must be named as an operator (not an approval gate)"

    def test_jeff_listed_as_operator(self):
        assert "Jeff Montgomery" in PROMPT, \
            "Jeff Montgomery must be named as an operator (not an approval gate)"

    def test_anti_pattern_signoff_gate_refused(self):
        # Prompt must name the anti-pattern explicitly so Cora refuses it
        assert "sign off" in PROMPT.lower() or "sign-off" in PROMPT.lower(), \
            "Prompt must name 'sign-off' as a refused anti-pattern"

    def test_escalate_to_harrison_rule(self):
        assert "escalate to Harrison" in PROMPT, \
            "Prompt must specify 'escalate to Harrison directly' as the correct pattern"

    def test_operators_not_approval_gates(self):
        assert "approval gates" in PROMPT or "NOT approval gates" in PROMPT or \
               "not a gate" in PROMPT.lower(), \
            "Prompt must state that managers are NOT approval gates"


# ---------------------------------------------------------------------------
# Category 2 #1 — Portfolio operating context
# ---------------------------------------------------------------------------

class TestPortfolioContext:
    """Current locked state that affects Cora behavior."""

    # UFL pause
    def test_ufl_pause_present(self):
        assert "paused" in PROMPT.lower(), \
            "UFL pause must be present in portfolio context"

    def test_ufl_pause_directive_date(self):
        assert "2026-05-10" in PROMPT, \
            "UFL pause directive date (2026-05-10) must be in prompt"

    def test_ufl_reengagement_criterion(self):
        assert "profitable" in PROMPT.lower(), \
            "UFL re-engagement criterion (profitable enough) must be stated in prompt"

    def test_ufl_no_outreach_no_new_spend(self):
        assert "outreach" in PROMPT.lower() or "new spend" in PROMPT.lower(), \
            "Prompt must prohibit new UFL outreach / spend"

    # F3 Energy
    def test_f3_pure_launch_date_615(self):
        assert "6/15" in PROMPT or "6/15/2026" in PROMPT or "June 15" in PROMPT, \
            "F3 Pure launch date 6/15/2026 must be in portfolio context"

    def test_f3_three_brand_architecture(self):
        assert "Pure" in PROMPT and "Mood" in PROMPT and "Energy" in PROMPT, \
            "All three F3 brands must be named in portfolio context"

    def test_f3_three_domains(self):
        assert "F3Pure.com" in PROMPT or "F3Mood.com" in PROMPT, \
            "Three-domain Shopify architecture must be in portfolio context"

    def test_bdm_production_layer_only(self):
        assert "production layer" in PROMPT.lower(), \
            "BDM as production-layer-only must be in portfolio context"

    # OSN
    def test_osn_four_stores(self):
        assert "GW" in PROMPT and "GM" in PROMPT and "GF" in PROMPT and "VVP" in PROMPT, \
            "All four OSN store codes must be in portfolio context"

    def test_osn_april_metrics_concerning(self):
        assert "45K" in PROMPT or "$(45K)" in PROMPT, \
            "OSN April accrual loss figure must be in portfolio context"

    def test_osn_breakeven_climb(self):
        assert "$240K" in PROMPT or "240K" in PROMPT, \
            "OSN breakeven climb to $240K must be in portfolio context"

    def test_osn_90_day_horizon(self):
        assert "90-day" in PROMPT or "90 day" in PROMPT, \
            "OSN 90-day operating horizon framing must be in portfolio context"

    # Rogers Ranch
    def test_rogers_ranch_present(self):
        assert "Rogers Ranch" in PROMPT, \
            "Rogers Ranch must be referenced in portfolio context"

    def test_rogers_ranch_airbnb_live(self):
        assert "Airbnb" in PROMPT, \
            "Rogers Ranch Airbnb live status must be in portfolio context"

    def test_mikenna_guest_ops(self):
        assert "Mikenna" in PROMPT, \
            "Mikenna anchoring guest ops must be in portfolio context"

    # Tessa
    def test_tessa_part_time_remote(self):
        assert "Tessa" in PROMPT, \
            "Tessa Miller transition must be in portfolio context"

    def test_tessa_not_departed(self):
        # Must clarify it's NOT a full departure
        assert "NOT a full departure" in PROMPT or "not a full departure" in PROMPT.lower() or \
               "part-time remote" in PROMPT, \
            "Tessa's status as part-time (NOT departed) must be explicit"

    def test_tessa_metrics_handoff_date(self):
        assert "5/26" in PROMPT, \
            "Tessa metrics + meeting structure handoff start date (5/26) must be in context"

    # AZ DDD deadline
    def test_az_ddd_deadline_present(self):
        assert "AZ DDD" in PROMPT or "DDD Therapy" in PROMPT or "AHCCCS" in PROMPT, \
            "AZ DDD Therapy Revalidation hard deadline must be in portfolio context"

    def test_az_ddd_deadline_date(self):
        assert "6/30" in PROMPT or "6/30/2026" in PROMPT or "2026-06-30" in PROMPT, \
            "AZ DDD 6/30 deadline date must be explicit in portfolio context"

    # Hannah payroll
    def test_hannah_payroll_100_hjrg(self):
        assert "100%" in PROMPT, \
            "Hannah's 100% HJR Global payroll allocation must be in portfolio context"

    def test_hannah_payroll_hjr_global(self):
        assert "Hannah" in PROMPT, \
            "Hannah Grant payroll allocation must be in portfolio context"

    # Gsheets connector
    def test_gsheets_connector_live(self):
        assert "gsheets" in PROMPT.lower() or "gsheets_financials" in PROMPT, \
            "gsheets_financials connector live status must be in portfolio context"

    def test_source_opacity_in_context_section(self):
        # Financial source-opacity rule must appear near gsheets context
        assert "no sheet names" in PROMPT or "source-opacity" in PROMPT.lower(), \
            "Source-opacity rule (no sheet names/links) must be in portfolio context"


# ---------------------------------------------------------------------------
# Category 5 #5 — Cross-entity confidentiality
# ---------------------------------------------------------------------------

class TestCrossEntityConfidentiality:
    """Entity-specific confidential data must not bleed across entity scopes."""

    def test_cross_entity_confidentiality_present(self):
        assert "cross-entity" in PROMPT.lower() or "confidential info" in PROMPT.lower(), \
            "Cross-entity confidentiality rule must be present in prompt"

    def test_f3_investor_terms_example(self):
        assert "F3 Energy investor" in PROMPT or "F3 Energy investor terms" in PROMPT, \
            "F3 Energy investor terms as a cross-entity example must be in prompt"

    def test_osn_apa_example(self):
        assert "OSN APA" in PROMPT, \
            "OSN APA financials as a cross-entity example must be in prompt"

    def test_hjrp_lease_example(self):
        assert "HJRP lease" in PROMPT or "lease terms" in PROMPT, \
            "HJRP lease terms as a cross-entity example must be in prompt"

    def test_aggregate_redirect_guidance(self):
        assert "aggregate" in PROMPT.lower(), \
            "Prompt must guide Cora to answer at aggregate level when cross-entity risk exists"


# ---------------------------------------------------------------------------
# What you do NOT do — anti-pattern bullets
# ---------------------------------------------------------------------------

class TestDoNotDoSection:
    """Anti-pattern bullets added in this refresh must be present."""

    def test_no_manager_signoff_bullet_present(self):
        assert "manager sign-off" in PROMPT.lower() or "sign-off gates" in PROMPT.lower() or \
               "Manager] to approve" in PROMPT, \
            "'Do not propose manager sign-off gates' bullet must be in 'What you do NOT do'"

    def test_visibility_cpa_bullet_present(self):
        assert "Visibility CPA team members as Slack recipients" in PROMPT or \
               "Visibility CPA" in PROMPT, \
            "'Do not include Visibility CPA members as recipients' bullet must be present"


# ---------------------------------------------------------------------------
# Regression — pre-existing guardrails must not have been dropped
# ---------------------------------------------------------------------------

class TestRegressionGuardrails:
    """Pre-existing sections must still be present after the 2026-05-24 refresh."""

    def test_who_you_are_section(self):
        assert "HJR portfolio of businesses" in PROMPT, \
            "Identity section must still be present"

    def test_voice_style_section(self):
        assert "Voice & style" in PROMPT or "Voice &amp; style" in PROMPT, \
            "Voice & style section must be present"

    def test_lead_with_answer_rule(self):
        assert "Lead with the answer" in PROMPT, \
            "Lead-with-answer voice rule must still be present"

    def test_no_data_source_naming(self):
        assert "don't have that right now" in PROMPT or "I don't have that right now" in PROMPT, \
            "Source-opacity refusal text must still be present"

    def test_financial_guardrail_tier1(self):
        assert "TIER_1" in PROMPT, \
            "Financial guardrail TIER_1 block must still be present"

    def test_financial_guardrail_tier3(self):
        assert "TIER_3" in PROMPT, \
            "Financial guardrail TIER_3 block must still be present"

    def test_financial_unknown_response_verbatim(self):
        assert (
            "I don't have that right now. I will notify the finance department"
            in PROMPT
        ), "Financial unknown-response verbatim text must still be present"

    def test_financial_get_cashflow_tool_referenced(self):
        assert "financial_get_cashflow" in PROMPT, \
            "financial_get_cashflow tool reference must still be in prompt"

    def test_ad_performance_section_present(self):
        assert "Ad performance" in PROMPT, \
            "Ad performance section must still be present"

    def test_ads_source_opacity_rule(self):
        assert "Source-opacity rule" in PROMPT or "source-opacity" in PROMPT.lower(), \
            "Ads source-opacity rule must still be present"

    def test_ads_tools_listed(self):
        for tool in [
            "ads_get_performance_summary",
            "ads_get_channel_breakdown",
            "ads_get_subbrand_performance",
            "ads_get_pixel_attribution",
            "ads_get_cm_waterfall",
        ]:
            assert tool in PROMPT, f"Ad tool '{tool}' must still be listed in prompt"

    def test_ads_f3e_scoped_only(self):
        assert "F3E-scoped only" in PROMPT, \
            "Ads tools must still be marked as F3E-scoped only"

    def test_sign_off_rule(self):
        assert "Sign-off" in PROMPT or "sign off" in PROMPT.lower(), \
            "Sign-off rule section must still be present"

    def test_no_cora_sign_off(self):
        assert "— Cora" in PROMPT, \
            "Prohibition against signing '— Cora' must still be in prompt"

    def test_edge_cases_section(self):
        assert "Edge cases" in PROMPT, \
            "Edge cases section must still be present"

    def test_holdco_framing(self):
        assert "holdco" in PROMPT.lower() or "HJR Global is the back office" in PROMPT, \
            "Holdco framing must still be present in identity section"
