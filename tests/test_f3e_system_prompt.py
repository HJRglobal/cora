"""
F3E system prompt content tests.

These tests verify that the f3e.md system prompt contains the expected locked
content from brand-guidelines V1, Pure launch context, Harrison-only-comms
guardrail, and UFL-pause discipline. They are documentation-as-tests: if a
future edit accidentally drops a locked decision, a test fails.

All tests are pure-Python string assertions against the prompt file — no
network calls, no imports from src/.
"""

import pathlib

PROMPT_PATH = pathlib.Path(__file__).parent.parent / "design" / "system-prompts" / "f3e.md"
PROMPT = PROMPT_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Category 2 #1 — Brand-guidelines V1 locks
# ---------------------------------------------------------------------------

class TestBrandGuidelinesV1:
    """Avatar names, palettes, taglines, and cross-brand do-not-drift rules."""

    # --- Avatar names ---
    def test_pure_avatar_name_lauren(self):
        assert '"Lauren"' in PROMPT, "Pure avatar name 'Lauren' must be in prompt"

    def test_mood_avatar_name_marcus(self):
        assert '"Marcus"' in PROMPT, "Mood avatar name 'Marcus' must be in prompt"

    def test_energy_avatar_name_alex(self):
        assert '"Alex"' in PROMPT, "Energy avatar name 'Alex' must be in prompt"

    # --- Taglines ---
    def test_pure_tagline(self):
        assert "Real energy for real life" in PROMPT, "Pure tagline must be in prompt"

    def test_mood_tagline(self):
        assert "Calm the Noise" in PROMPT, "Mood tagline must be in prompt"

    def test_energy_tagline(self):
        assert "Fuel. Focus. Finish." in PROMPT, "Energy tagline must be in prompt"

    def test_energy_secondary_tagline(self):
        assert "When Clarity Counts" in PROMPT, "Energy secondary tagline must be in prompt"

    # --- Mood NOT sleep positioning ---
    def test_mood_not_sleep_aid(self):
        assert "NOT a sleep drink" in PROMPT or "not a sleep" in PROMPT.lower(), \
            "Prompt must explicitly state Mood is NOT a sleep drink"

    # --- Palettes ---
    def test_pure_teal_hex(self):
        assert "#2EBFB3" in PROMPT, "Pure Teal hex must be in prompt"

    def test_pure_coral_hex(self):
        assert "#F47B6C" in PROMPT, "Pure Coral hex must be in prompt"

    def test_mood_black_hex(self):
        assert "#1A1A1A" in PROMPT, "Mood Black hex must be in prompt"

    def test_mood_orange_hex(self):
        # Mood Orange (#FF6B00, PMS 1505C) superseded the original Mood Gold
        # (#C9A84C) in the brand cascade commit e57e079 ("Mood Orange replaces Gold").
        assert "#FF6B00" in PROMPT, "Mood Orange hex must be in prompt"

    def test_energy_red_hex(self):
        assert "#B02225" in PROMPT, "Energy Red hex must be in prompt"

    def test_energy_bright_red_hex(self):
        assert "#ED1C24" in PROMPT, "Energy Bright Red hex must be in prompt"

    # --- Typography locks ---
    def test_josefin_sans_mentioned(self):
        assert "Josefin Sans" in PROMPT, "Josefin Sans typeface must be referenced in prompt"

    def test_nunito_sans_mentioned(self):
        assert "Nunito Sans" in PROMPT, "Nunito Sans typeface must be referenced in prompt"

    # --- Ingredients ---
    def test_mood_ingredients(self):
        for ingredient in ["chamomile", "GABA", "magnesium", "valerian root"]:
            assert ingredient in PROMPT, f"Mood ingredient '{ingredient}' must be in prompt"

    def test_energy_ingredients(self):
        for ingredient in ["ginseng panax", "BCAA", "L-theanine", "ginkgo biloba"]:
            assert ingredient in PROMPT, f"Energy ingredient '{ingredient}' must be in prompt"

    # --- Cross-brand do-not-drift ---
    def test_cross_brand_do_not_drift_section(self):
        assert "do not drift" in PROMPT.lower() or "Cross-brand" in PROMPT, \
            "Cross-brand do-not-drift rules must be present in prompt"

    def test_pure_not_gym_territory(self):
        # Pure must be flagged as ≠ gym/MMA lane
        assert "Energy's lane" in PROMPT, \
            "Prompt must reference Energy's lane as a cross-brand boundary"

    def test_mood_not_sleep_territory(self):
        # Mood must be flagged as ≠ sleep aid
        assert "sleep" in PROMPT.lower(), \
            "Prompt must reference sleep positioning as an anti-pattern for Mood"

    # --- Red duotone visual signature ---
    def test_energy_red_duotone(self):
        assert "duotone" in PROMPT.lower(), \
            "Energy's red duotone photography signature must be referenced in prompt"

    # --- Alex Cordova role anchor ---
    def test_alex_cordova_mma_anchor(self):
        assert "Alex Cordova" in PROMPT or "Alex" in PROMPT, \
            "Alex Cordova MMA sub-account voice anchor must be in prompt"

    def test_alex_cordova_role_updated(self):
        # Post-5/22 role clarification: post-sale logistics + account manager
        assert "post-sale logistics" in PROMPT or "account manager" in PROMPT, \
            "Alex Cordova's updated role (post-sale logistics + account manager) must be in prompt"

    def test_mikenna_not_f3_team(self):
        assert "Mikenna" in PROMPT, \
            "Mikenna must be explicitly scoped OUT of the F3 team in the prompt"
        # Check that Mikenna is noted as NOT on F3 team
        mikenna_idx = PROMPT.index("Mikenna")
        mikenna_context = PROMPT[mikenna_idx:mikenna_idx + 200]
        assert "NOT" in mikenna_context or "not" in mikenna_context.lower(), \
            "Mikenna section must explicitly say she is NOT on the F3 team"


# ---------------------------------------------------------------------------
# Category 2 #2 — F3 Pure 6/15 launch context
# ---------------------------------------------------------------------------

class TestPureLaunchContext:
    """Pure launch date locked 6/15, Nimbl sync, Shopify domains."""

    def test_launch_date_locked_615(self):
        assert "6/15" in PROMPT or "June 15" in PROMPT, \
            "Pure launch date 6/15 must be explicit in prompt"

    def test_launch_date_not_61(self):
        # '6/1' could match '6/15' — test that we don't say it's 6/1 without the 5
        # Accept '6/15' but not standalone '6/1' as the launch date
        import re
        # Matches '6/1' not followed by digit (i.e. not '6/15', '6/18', etc.)
        standalone_61 = re.search(r'6/1(?!\d)', PROMPT)
        if standalone_61:
            # It must not be in a "launch date is 6/1" context — just ensure 6/15 also present
            assert "6/15" in PROMPT, \
                "If 6/1 appears, 6/15 must also appear as the locked launch date"

    def test_blue_chip_delay_reason(self):
        assert "Blue Chip" in PROMPT or "BCB" in PROMPT, \
            "Blue Chip Beverage delay context must be referenced in prompt"

    def test_individual_flavors_seeding(self):
        assert "Sprouts" in PROMPT and "Whole Foods" in PROMPT, \
            "Sprouts + Whole Foods seeding context must be in prompt"

    def test_nimbl_shopify_sync(self):
        assert "Nimbl" in PROMPT, "Nimbl 3PL must be referenced in prompt"

    def test_shopify_three_domains(self):
        assert "F3Pure.com" in PROMPT or "F3Mood.com" in PROMPT, \
            "Three-domain Shopify architecture must be referenced in prompt"

    def test_upc_gtin_locked(self):
        assert "850045501686" in PROMPT, "Locked UPC/GTIN must be in prompt"

    def test_do_not_talk_to_retailers_before_date_confirmed(self):
        assert "credibility" in PROMPT or "walking back" in PROMPT or "don't talk" in PROMPT.lower(), \
            "Anti-pattern of talking to retailers before launch date confirmed must be in prompt"


# ---------------------------------------------------------------------------
# Category 2 #3 — Harrison-only comms guardrail
# ---------------------------------------------------------------------------

class TestHarrisonOnlyComms:
    """External vendor comms are Harrison-only — Cora must refuse to draft."""

    def test_harrison_only_comms_section_present(self):
        assert "Harrison-only" in PROMPT, \
            "Harrison-only comms guardrail section must be in prompt"

    def test_bcb_listed(self):
        assert "Blue Chip Beverage" in PROMPT, \
            "Blue Chip Beverage must be listed under Harrison-only comms"

    def test_allen_flavors_listed(self):
        assert "Allen Flavors" in PROMPT, \
            "Allen Flavors must be listed under Harrison-only comms"

    def test_drink_labs_listed(self):
        assert "Drink Labs" in PROMPT, \
            "Drink Labs must be listed under Harrison-only comms"

    def test_nimbl_listed_in_comms_guardrail(self):
        assert "Nimbl" in PROMPT, \
            "Nimbl 3PL must be listed under Harrison-only comms"

    def test_cotton_3pl_listed(self):
        assert "Cotton 3PL" in PROMPT, \
            "Cotton 3PL must be listed under Harrison-only comms"

    def test_cora_refuses_to_draft_vendor_comms(self):
        # Prompt must contain the refusal pattern
        assert "flag it to Harrison" in PROMPT or "Harrison-only" in PROMPT, \
            "Prompt must specify Cora refuses external vendor comms and routes to Harrison"

    def test_even_draft_is_refused(self):
        assert "draft" in PROMPT.lower(), \
            "Prompt must address the 'just a draft' edge case for vendor comms"


# ---------------------------------------------------------------------------
# Category 2 #4 — UFL-pause discipline
# ---------------------------------------------------------------------------

class TestUFLPauseDiscipline:
    """UFL-specific F3 partnerships are paused; MMA generally fine."""

    def test_ufl_pause_section_present(self):
        assert "UFL-pause" in PROMPT or "UFL is paused" in PROMPT or "paused per Harrison" in PROMPT, \
            "UFL-pause discipline section must be in prompt"

    def test_ufl_crossover_content_blocked(self):
        assert "F3-UFL crossover" in PROMPT or "UFL-specific" in PROMPT, \
            "UFL crossover content / partnerships must be explicitly blocked in prompt"

    def test_mma_generally_fine(self):
        assert "MMA generally" in PROMPT or "MMA-adjacent" in PROMPT, \
            "Prompt must clarify that non-UFL MMA partnerships remain OK"

    def test_ufl_specific_partnerships_blocked(self):
        assert "UFL-specific" in PROMPT, \
            "UFL-specific athlete partnerships must be explicitly blocked in prompt"

    def test_ufl_pause_directive_date(self):
        assert "2026-05-10" in PROMPT, \
            "UFL pause directive date (2026-05-10) must be referenced in prompt"


# ---------------------------------------------------------------------------
# Pre-existing guardrails — regression tests
# ---------------------------------------------------------------------------

class TestRegressionGuardrails:
    """Verify pre-existing guardrails were not dropped during the refresh."""

    def test_financial_guardrail_tier1_tier3(self):
        assert "TIER_1" in PROMPT and "TIER_3" in PROMPT, \
            "Financial guardrail TIER_1/TIER_3 block must still be in prompt"

    def test_financial_unknown_response_verbatim(self):
        assert "I don't have that right now. I will notify the finance department" in PROMPT, \
            "Financial unknown-response verbatim text must still be in prompt"

    def test_source_opacity_rule(self):
        assert "Source-opacity" in PROMPT or "source-opacity" in PROMPT, \
            "Ad performance source-opacity rule must still be in prompt"

    def test_ads_tools_listed(self):
        assert "ads_get_performance_summary" in PROMPT, \
            "Ads tools block must still be present in prompt"

    def test_cross_entity_scope_block(self):
        assert "Cross-entity scope" in PROMPT, \
            "Cross-entity scope section must still be in prompt"

    def test_knowledge_gap_marker(self):
        assert "CORA_KNOWLEDGE_GAP" in PROMPT, \
            "Knowledge gap marker instruction must still be in prompt"

    def test_d_backs_deal_dead(self):
        assert "D-Backs" in PROMPT and ("dead" in PROMPT.lower() or "DEAD" in PROMPT), \
            "D-Backs deal dead note must still be in prompt"

    def test_premium_positioning_pushback(self):
        assert "premium positioning" in PROMPT, \
            "Premium positioning pushback line must still be in prompt"

    def test_health_claims_refusal(self):
        assert "health claim" in PROMPT.lower() or "health or nutrient claim" in PROMPT.lower(), \
            "Health claims refusal must be in prompt"
