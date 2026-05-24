"""Tests for the f3e_brand_voice_check brand voice client.

Pure pattern-matching assertions — no network calls, no Claude API calls, no src/ imports
beyond the brand_voice_client module under test.

Coverage:
  1. Mood sleep positioning — CRITICAL severity, non-negotiable anti-pattern
  2. Energy-lane drift in Pure copy
  3. Mood-lane drift in Pure copy
  4. Mood Energy-lane drift
  5. Mood Pure-lane drift
  6. Energy anti-positioning (competitors, cross-entity)
  7. Energy Mood-lane drift
  8. Energy Pure-lane drift
  9. Health/nutrition claims — universal across all three brands
 10. UFL pause — cross-entity, universal
 11. Clean copy passes without false positives
 12. Invalid brand returns CRITICAL finding
 13. Verdict helpers
 14. format_result_for_llm output structure
 15. Dispatch-layer integration — _tool_f3e_brand_voice_check via tool_dispatch
"""

import sys
from pathlib import Path

# Ensure src/ is on sys.path for direct imports
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.tools import brand_voice_client
from cora.tools.brand_voice_client import (
    BrandCheckResult,
    Finding,
    check_copy,
    format_result_for_llm,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _has_critical(result: BrandCheckResult, substr: str | None = None) -> bool:
    crits = [f for f in result.findings if f.severity == "CRITICAL"]
    if substr is None:
        return len(crits) > 0
    return any(substr.lower() in f.term_found.lower() or substr.lower() in f.message.lower() for f in crits)


def _has_warning(result: BrandCheckResult, substr: str | None = None) -> bool:
    warns = [f for f in result.findings if f.severity == "WARNING"]
    if substr is None:
        return len(warns) > 0
    return any(substr.lower() in f.term_found.lower() or substr.lower() in f.message.lower() for f in warns)


def _categories(result: BrandCheckResult) -> list[str]:
    return [f.category for f in result.findings]


# ─── Category 1: Mood sleep positioning — CRITICAL ────────────────────────────


class TestMoodSleepPositioning:
    """Mood must NEVER be positioned as a sleep drink — CRITICAL anti-pattern.

    Each test exercises one of the banned sleep-adjacent terms.
    """

    def test_sleep_support_is_critical(self):
        result = check_copy("mood", "F3 Mood offers sleep support after long shifts.")
        assert _has_critical(result, "sleep support"), "sleep support must be CRITICAL in Mood copy"

    def test_sleep_aid_is_critical(self):
        result = check_copy("mood", "This is the sleep aid you didn't know you needed.")
        assert _has_critical(result, "sleep aid"), "sleep aid must be CRITICAL in Mood copy"

    def test_helps_you_sleep_is_critical(self):
        result = check_copy("mood", "F3 Mood helps you sleep after a brutal ER shift.")
        assert _has_critical(result, "helps you sleep"), "helps you sleep must be CRITICAL"

    def test_helps_with_sleep_is_critical(self):
        result = check_copy("mood", "Try Mood — helps with sleep.")
        assert _has_critical(result, "helps with sleep"), "helps with sleep must be CRITICAL"

    def test_better_sleep_is_critical(self):
        result = check_copy("mood", "Wake up refreshed. Better sleep starts with Mood.")
        assert _has_critical(result, "better sleep"), "better sleep must be CRITICAL"

    def test_melatonin_is_critical(self):
        result = check_copy("mood", "No melatonin needed — F3 Mood gets the job done.")
        assert _has_critical(result, "melatonin"), "melatonin must be CRITICAL (not in formula + sleep positioning)"

    def test_bedtime_is_critical(self):
        result = check_copy("mood", "Add to your bedtime routine.")
        assert _has_critical(result, "bedtime"), "bedtime must be CRITICAL in Mood copy"

    def test_nightcap_is_critical(self):
        result = check_copy("mood", "The perfect nightcap for the end of a long day.")
        assert _has_critical(result, "nightcap"), "nightcap must be CRITICAL in Mood copy"

    def test_night_cap_two_words_is_critical(self):
        result = check_copy("mood", "Consider it your night cap.")
        assert _has_critical(result, "night cap"), "night cap (two words) must be CRITICAL in Mood copy"

    def test_before_bed_is_critical(self):
        result = check_copy("mood", "Drink F3 Mood before bed for a calmer tomorrow.")
        assert _has_critical(result, "before bed"), "before bed must be CRITICAL in Mood copy"

    def test_sleep_bare_word_is_critical(self):
        result = check_copy("mood", "Helps you get the sleep you need.")
        assert _has_critical(result, "sleep"), "bare 'sleep' in Mood copy must be CRITICAL"

    def test_drowsy_is_critical(self):
        result = check_copy("mood", "Wake up less drowsy with Mood's calming formula.")
        assert _has_critical(result, "drowsy"), "drowsy must be CRITICAL in Mood copy"

    def test_sleepy_is_critical(self):
        result = check_copy("mood", "Never feel sleepy at your desk again.")
        assert _has_critical(result, "sleepy"), "sleepy must be CRITICAL in Mood copy"

    def test_sleep_findings_have_correct_category(self):
        result = check_copy("mood", "This sleep support drink helps you relax before bed.")
        cats = _categories(result)
        assert any("Sleep positioning" in c for c in cats), (
            "Sleep positioning findings must have 'Sleep positioning' in category"
        )

    def test_sleep_verdict_needs_revision(self):
        result = check_copy("mood", "Your bedtime companion.")
        assert "NEEDS REVISION" in result.verdict, "Sleep positioning must trigger NEEDS REVISION verdict"


# ─── Category 2: Energy-lane drift in Pure copy ───────────────────────────────


class TestPureEnergyDrift:
    """Pure copy must not use gym/MMA/performance framing — that's Energy's lane."""

    def test_pre_workout_flagged_in_pure(self):
        result = check_copy("pure", "The best pre-workout fuel for your morning run.")
        assert _has_warning(result, "pre-workout"), "pre-workout must be flagged as Energy-lane drift in Pure"

    def test_beast_mode_flagged_in_pure(self):
        result = check_copy("pure", "Go beast mode with F3 Pure.")
        assert _has_warning(result, "beast mode"), "beast mode must be flagged in Pure"

    def test_gains_flagged_in_pure(self):
        result = check_copy("pure", "See gains from day one with F3 Pure.")
        assert _has_warning(result, "gains"), "gains must be flagged in Pure"

    def test_mma_flagged_in_pure(self):
        result = check_copy("pure", "Fuel your MMA training with F3 Pure.")
        assert _has_warning(result, "mma"), "MMA language must be flagged in Pure"

    def test_knockout_flagged_in_pure(self):
        result = check_copy("pure", "A knockout flavor that keeps you going.")
        assert _has_warning(result, "knockout"), "knockout must be flagged in Pure"

    def test_energy_drift_category_label(self):
        result = check_copy("pure", "The pre-workout choice for real athletes.")
        cats = _categories(result)
        assert any("Energy-lane drift" in c for c in cats), (
            "Energy-lane drift findings must use 'Energy-lane drift' category"
        )


# ─── Category 3: Mood-lane drift in Pure copy ────────────────────────────────


class TestPureMoodDrift:
    """Pure copy must not borrow Mood's calming/anxiety-relief framing."""

    def test_calming_flagged_in_pure(self):
        result = check_copy("pure", "A calming boost for your morning.")
        assert _has_warning(result, "calming"), "calming must be flagged as Mood-lane drift in Pure"

    def test_anxiety_flagged_in_pure(self):
        result = check_copy("pure", "Helps with anxiety before big presentations.")
        assert _has_warning(result, "anxiety"), "anxiety must be flagged in Pure"

    def test_stress_relief_flagged_in_pure(self):
        result = check_copy("pure", "The stress relief drink for busy moms.")
        assert _has_warning(result, "stress relief"), "stress relief must be flagged in Pure"

    def test_relax_flagged_in_pure(self):
        result = check_copy("pure", "Relax and recharge with F3 Pure.")
        assert _has_warning(result, "relax"), "relax must be flagged as Mood drift in Pure"

    def test_wind_down_flagged_in_pure(self):
        result = check_copy("pure", "Wind down after your workout with F3 Pure.")
        assert _has_warning(result, "wind down"), "wind down must be flagged in Pure"

    def test_mood_drift_category_label(self):
        result = check_copy("pure", "A calming energy for your daily wind down.")
        cats = _categories(result)
        assert any("Mood-lane drift" in c for c in cats), (
            "Mood-lane drift findings must use 'Mood-lane drift' category"
        )


# ─── Category 4: Mood Energy-lane drift ──────────────────────────────────────


class TestMoodEnergyDrift:
    """Mood is not a pre-workout or gym drink — Energy-lane drift in Mood copy."""

    def test_pre_workout_flagged_in_mood(self):
        result = check_copy("mood", "The perfect pre-workout to sharpen your focus.")
        assert _has_warning(result, "pre-workout"), "pre-workout must be flagged as Energy drift in Mood"

    def test_beast_mode_flagged_in_mood(self):
        result = check_copy("mood", "Beast mode starts with F3 Mood.")
        assert _has_warning(result, "beast mode"), "beast mode must be flagged in Mood"

    def test_mma_flagged_in_mood(self):
        result = check_copy("mood", "Trusted by MMA athletes everywhere.")
        assert _has_warning(result, "mma"), "MMA must be flagged in Mood copy"

    def test_gains_flagged_in_mood(self):
        result = check_copy("mood", "Build mental and physical gains with Mood.")
        assert _has_warning(result, "gains"), "gains must be flagged in Mood copy"


# ─── Category 5: Mood Pure-lane drift ────────────────────────────────────────


class TestMoodPureDrift:
    """Mood must not borrow Pure's natural/clean/accessible framing."""

    def test_all_natural_flagged_in_mood(self):
        result = check_copy("mood", "All natural ingredients for a calmer mind.")
        assert _has_warning(result, "all natural"), "all natural must be flagged as Pure drift in Mood"

    def test_organic_flagged_in_mood(self):
        result = check_copy("mood", "Organic chamomile keeps you focused.")
        assert _has_warning(result, "organic"), "organic must be flagged in Mood"

    def test_no_artificial_flagged_in_mood(self):
        result = check_copy("mood", "No artificial colors, no compromises.")
        assert _has_warning(result, "no artificial"), "no artificial must be flagged in Mood as Pure drift"

    def test_for_the_whole_family_flagged_in_mood(self):
        result = check_copy("mood", "Designed for the whole family, including kids.")
        assert _has_warning(result, "for the whole family"), "for the whole family must be flagged in Mood"


# ─── Category 6: Energy anti-positioning ────────────────────────────────────


class TestEnergyAntiPositioning:
    """Energy must not name competitors or use generic bro-y language."""

    def test_red_bull_flagged_in_energy(self):
        result = check_copy("energy", "Unlike Red Bull, F3 Energy gives you real focus.")
        assert _has_critical(result, "red bull"), "red bull must be CRITICAL in Energy copy"

    def test_monster_energy_flagged_in_energy(self):
        result = check_copy("energy", "Monster Energy wishes it had our formula.")
        assert _has_critical(result, "monster energy"), "monster energy must be CRITICAL in Energy copy"

    def test_bang_energy_flagged_in_energy(self):
        result = check_copy("energy", "We're not Bang Energy — we're better.")
        assert _has_critical(result, "bang energy"), "bang energy must be CRITICAL in Energy copy"

    def test_celsius_flagged_in_energy(self):
        result = check_copy("energy", "Better than Celsius for your workout.")
        assert _has_critical(result, "celsius"), "celsius must be CRITICAL in Energy copy"

    def test_ufc_flagged_in_energy(self):
        result = check_copy("energy", "Official drink of UFC training camps.")
        assert _has_critical(result, "ufc"), "ufc brand name must be CRITICAL in Energy copy"

    def test_bro_language_flagged_in_energy(self):
        result = check_copy("energy", "Hey bro, this drink hits different.")
        assert _has_critical(result, "bro"), "bro language must be CRITICAL in Energy copy"

    def test_shredded_flagged_in_energy(self):
        result = check_copy("energy", "Get shredded with F3 Energy.")
        assert _has_critical(result, "shredded"), "shredded must be CRITICAL in Energy copy"

    def test_anti_positioning_category_label(self):
        result = check_copy("energy", "Red Bull can't compete with our formula.")
        cats = _categories(result)
        assert any("Anti-positioning" in c for c in cats), (
            "Competitor brand name findings must use 'Anti-positioning' category"
        )

    def test_competitor_verdict(self):
        result = check_copy("energy", "Better than Red Bull and Monster Energy combined.")
        assert "NEEDS REVISION" in result.verdict, "Competitor brand name must trigger NEEDS REVISION verdict"


# ─── Category 7: Energy Mood-lane drift ──────────────────────────────────────


class TestEnergyMoodDrift:
    """Energy copy must not borrow Mood's calming/anxiety-relief language."""

    def test_calming_flagged_in_energy(self):
        result = check_copy("energy", "A calming boost for intense workouts.")
        assert _has_warning(result, "calming"), "calming must be flagged as Mood drift in Energy"

    def test_calm_the_noise_tagline_flagged_in_energy(self):
        result = check_copy("energy", "Calm the Noise — that's what we're about.")
        assert _has_warning(result, "calm the noise"), "Mood's trademarked tagline must be flagged in Energy copy"

    def test_anxiety_relief_flagged_in_energy(self):
        result = check_copy("energy", "Anxiety relief for competitive fighters.")
        assert _has_warning(result, "anxiety relief"), "anxiety relief must be flagged as Mood drift in Energy"

    def test_stress_relief_flagged_in_energy(self):
        result = check_copy("energy", "Stress relief meets performance.")
        assert _has_warning(result, "stress relief"), "stress relief must be flagged in Energy"

    def test_wind_down_flagged_in_energy(self):
        result = check_copy("energy", "Wind down after the fight with F3 Energy.")
        assert _has_warning(result, "wind down"), "wind down must be flagged as Mood drift in Energy"


# ─── Category 8: Energy Pure-lane drift ──────────────────────────────────────


class TestEnergyPureDrift:
    """Energy must not over-soften into Pure's gentle/accessible/natural lane."""

    def test_gentle_energy_flagged_in_energy(self):
        result = check_copy("energy", "Gentle energy that works all day long.")
        assert _has_warning(result, "gentle energy"), "gentle energy must be flagged as Pure drift in Energy"

    def test_clean_energy_for_everyone_flagged_in_energy(self):
        result = check_copy("energy", "Clean energy for everyone in the gym.")
        assert _has_warning(result, "clean energy for everyone"), (
            "clean energy for everyone must be flagged as Pure drift in Energy"
        )

    def test_for_the_whole_family_flagged_in_energy(self):
        result = check_copy("energy", "F3 Energy is for the whole family.")
        assert _has_warning(result, "for the whole family"), "for the whole family must be flagged in Energy"

    def test_light_energy_flagged_in_energy(self):
        result = check_copy("energy", "Light energy for any time of day.")
        assert _has_warning(result, "light energy"), "light energy must be flagged as Pure drift in Energy"

    def test_natural_choice_flagged_in_energy(self):
        result = check_copy("energy", "The natural choice for fighters.")
        assert _has_warning(result, "natural choice"), "natural choice must be flagged in Energy"


# ─── Category 9: Health/nutrition claims (universal) ─────────────────────────


class TestHealthClaims:
    """Health and nutrient claims must be flagged as CRITICAL in all three brands."""

    def test_clinically_proven_is_critical_in_pure(self):
        result = check_copy("pure", "Clinically proven to improve your energy levels.")
        assert _has_critical(result, "clinically"), "clinically proven must be CRITICAL in Pure"

    def test_clinically_tested_is_critical_in_mood(self):
        result = check_copy("mood", "Clinically tested for professional focus.")
        assert _has_critical(result, "clinically"), "clinically tested must be CRITICAL in Mood"

    def test_clinically_shown_is_critical_in_energy(self):
        result = check_copy("energy", "Clinically shown to enhance performance.")
        assert _has_critical(result, "clinically"), "clinically shown must be CRITICAL in Energy"

    def test_fda_approved_is_critical(self):
        result = check_copy("pure", "FDA approved formula for peak performance.")
        assert _has_critical(result, "FDA"), "FDA approved must be CRITICAL"

    def test_immune_boost_is_critical(self):
        result = check_copy("energy", "Boosts your immune system for peak training.")
        assert _has_critical(result, "immune"), "immune boost claim must be CRITICAL"

    def test_health_claim_category_label(self):
        result = check_copy("mood", "Clinically proven to reduce anxiety.")
        cats = _categories(result)
        assert any("Health" in c for c in cats), (
            "Health claim findings must include 'Health' in category name"
        )

    def test_health_claim_applies_to_all_brands(self):
        for brand in ("pure", "mood", "energy"):
            result = check_copy(brand, "Clinically proven to improve cognitive function.")
            assert _has_critical(result), f"Health claim must be CRITICAL for brand={brand}"


# ─── Category 10: UFL pause — cross-entity (universal) ───────────────────────


class TestUFLPause:
    """F3-UFL crossover content is blocked per 2026-05-10 directive — all three brands."""

    def test_ufl_flagged_in_pure(self):
        result = check_copy("pure", "Official drink of the UFL.")
        assert _has_critical(result, "ufl"), "UFL must be CRITICAL in Pure copy"

    def test_ufl_flagged_in_mood(self):
        result = check_copy("mood", "Trusted by UFL athletes to stay focused.")
        assert _has_critical(result, "ufl"), "UFL must be CRITICAL in Mood copy"

    def test_ufl_flagged_in_energy(self):
        result = check_copy("energy", "The official energy drink of the UFL.")
        assert _has_critical(result, "ufl"), "UFL must be CRITICAL in Energy copy"

    def test_united_fight_league_flagged(self):
        result = check_copy("energy", "Partnered with the United Fight League for 2026.")
        assert _has_critical(result, "united fight league"), "United Fight League must be CRITICAL"

    def test_ufl_category_label(self):
        result = check_copy("pure", "UFL athletes trust F3 Pure.")
        cats = _categories(result)
        assert any("Cross-entity" in c for c in cats), (
            "UFL pause findings must use 'Cross-entity' category"
        )


# ─── Category 11: Clean copy passes without false positives ──────────────────


class TestCleanCopyPasses:
    """Well-written on-brand copy should not trigger false positives."""

    def test_clean_pure_copy_passes(self):
        copy = (
            "Real energy for real life. F3 Pure gives Lauren the clean, everyday boost "
            "she needs — whether she's at the farmers market, chasing her kids, or "
            "heading to her morning Pilates class. Real energy for real people."
        )
        result = check_copy("pure", copy)
        assert not result.findings, (
            f"Clean Pure copy should have no findings but got: {result.findings}"
        )

    def test_clean_mood_copy_passes(self):
        copy = (
            "Calm the Noise.™ When the ER never stops and the clock says midnight, "
            "Marcus reaches for F3 Mood. Chamomile, GABA, magnesium, valerian root — "
            "not to slow him down, but to cut through the noise so he can stay sharp. "
            "For the professionals who can't afford to lose their edge."
        )
        result = check_copy("mood", copy)
        # Should have no critical findings; may have no findings at all
        assert not result.has_critical, (
            f"Clean Mood copy should have no CRITICAL findings but got: "
            f"{[f for f in result.findings if f.severity == 'CRITICAL']}"
        )

    def test_clean_energy_copy_passes(self):
        copy = (
            "Fuel. Focus. Finish. F3 Energy is built for the fight — not the gym-bro show. "
            "Alex Cordova trains on it. Ginseng panax, L-theanine, ginkgo biloba. "
            "When clarity counts, F3 Energy delivers."
        )
        result = check_copy("energy", copy)
        # 'gym-bro' contains 'bro' — legitimate test of whether the check fires
        # The copy uses 'bro' in the phrase 'gym-bro show' — this IS a valid flag per rules
        # so we only check that there are no CRITICAL findings outside the bro detection
        non_bro_criticals = [
            f for f in result.findings
            if f.severity == "CRITICAL" and "bro" not in f.term_found.lower()
        ]
        assert not non_bro_criticals, (
            f"Clean Energy copy should have no non-bro CRITICAL findings but got: {non_bro_criticals}"
        )

    def test_mood_mma_copy_no_false_positive(self):
        """'MMA Lab' sub-account partner content is Energy territory, not Mood —
        but this test verifies Mood copy WITHOUT MMA passes cleanly."""
        copy = (
            "The calm before the case. F3 Mood. For the ones who face pressure "
            "every day — not on the mat, but in the courtroom, the operating room, "
            "the field. Calm the Noise.™"
        )
        result = check_copy("mood", copy)
        assert not result.has_critical, (
            "On-brand Mood copy without sleep language should have no CRITICAL findings"
        )

    def test_pure_fitness_language_without_gym_framing_passes(self):
        """Pure can reference movement and energy — just not gym/MMA language."""
        copy = (
            "Keep moving. F3 Pure fits your morning run, your afternoon hike, "
            "your Tuesday Pilates class. Real energy for real life."
        )
        result = check_copy("pure", copy)
        assert not result.findings, (
            f"Movement-positive Pure copy without gym language should pass: {result.findings}"
        )


# ─── Category 12: Invalid brand error handling ────────────────────────────────


class TestInvalidBrand:
    """Invalid brand values must return a CRITICAL finding, not raise an exception."""

    def test_unknown_brand_returns_critical(self):
        result = check_copy("f3pure", "Some copy here.")
        assert result.has_critical, "Unknown brand must yield a CRITICAL finding"

    def test_empty_brand_returns_critical(self):
        result = check_copy("", "Some copy here.")
        assert result.has_critical, "Empty brand must yield a CRITICAL finding"

    def test_unknown_brand_category_is_tool_error(self):
        result = check_copy("notabrand", "Copy text.")
        cats = _categories(result)
        assert any("error" in c.lower() for c in cats), (
            "Unknown brand finding must use 'Tool error' or similar category"
        )


# ─── Category 13: Verdict helpers ────────────────────────────────────────────


class TestVerdictHelpers:
    """BrandCheckResult.verdict and has_critical/has_warning properties."""

    def test_verdict_needs_revision_on_critical(self):
        result = BrandCheckResult(brand="mood", copy_preview="test")
        result.findings.append(
            Finding(severity="CRITICAL", category="test", term_found="sleep", message="test")
        )
        assert "NEEDS REVISION" in result.verdict

    def test_verdict_review_on_warning_only(self):
        result = BrandCheckResult(brand="pure", copy_preview="test")
        result.findings.append(
            Finding(severity="WARNING", category="test", term_found="relax", message="test")
        )
        assert "REVIEW BEFORE POSTING" in result.verdict

    def test_verdict_passes_on_no_findings(self):
        result = BrandCheckResult(brand="energy", copy_preview="test")
        assert "PASSES" in result.verdict

    def test_has_critical_false_when_no_criticals(self):
        result = BrandCheckResult(brand="pure", copy_preview="test")
        result.findings.append(
            Finding(severity="WARNING", category="test", term_found="x", message="y")
        )
        assert not result.has_critical

    def test_has_warning_false_when_no_warnings(self):
        result = BrandCheckResult(brand="pure", copy_preview="test")
        assert not result.has_warning


# ─── Category 14: format_result_for_llm output structure ─────────────────────


class TestFormatResultForLLM:
    """format_result_for_llm must produce structured text with expected sections."""

    def test_clean_output_contains_verdict(self):
        result = check_copy("pure", "Real energy for real life. A great day starts here.")
        output = format_result_for_llm(result)
        assert "VERDICT" in output, "Output must contain VERDICT section"

    def test_clean_output_contains_voice_spec(self):
        result = check_copy("pure", "Real energy for real life.")
        output = format_result_for_llm(result)
        assert "VOICE SPEC" in output, "Output must contain VOICE SPEC section"

    def test_critical_output_contains_critical_emoji(self):
        result = check_copy("mood", "This sleep aid helps you wind down for bed.")
        output = format_result_for_llm(result)
        assert "🚨" in output or "CRITICAL" in output, (
            "Output with CRITICAL findings must flag them prominently"
        )

    def test_warning_output_contains_warning_marker(self):
        result = check_copy("pure", "Go beast mode with your morning workout.")
        output = format_result_for_llm(result)
        assert "⚠️" in output or "WARNING" in output, (
            "Output with WARNING findings must flag them"
        )

    def test_output_includes_brand_header(self):
        for brand in ("pure", "mood", "energy"):
            result = check_copy(brand, "Some copy for this brand.")
            output = format_result_for_llm(result)
            assert brand.upper() in output.upper(), (
                f"Output must include brand name {brand.upper()}"
            )

    def test_output_includes_copy_preview(self):
        copy = "Real energy for real life — that's F3 Pure."
        result = check_copy("pure", copy)
        output = format_result_for_llm(result)
        assert copy[:30] in output, "Output must include copy preview"

    def test_output_contains_findings_count(self):
        result = check_copy("mood", "Sleep support for weary doctors.")
        output = format_result_for_llm(result)
        assert "FINDINGS" in output, "Output must include FINDINGS section"

    def test_note_about_pattern_matching_present(self):
        result = check_copy("energy", "Fuel. Focus. Finish.")
        output = format_result_for_llm(result)
        assert "pattern" in output.lower(), "Output must include caveat about pattern-only checking"


# ─── Category 15: Dispatch-layer integration ─────────────────────────────────


class TestDispatchIntegration:
    """_tool_f3e_brand_voice_check wired correctly in tool_dispatch."""

    def test_tool_registered_in_dispatch(self):
        from cora.tools.tool_dispatch import _TOOL_FUNCTIONS
        assert "f3e_brand_voice_check" in _TOOL_FUNCTIONS, (
            "f3e_brand_voice_check must be registered in _TOOL_FUNCTIONS"
        )

    def test_tool_definition_present(self):
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert "f3e_brand_voice_check" in names, (
            "f3e_brand_voice_check must be in TOOL_DEFINITIONS"
        )

    def test_tool_definition_has_required_fields(self):
        from cora.tools.tool_dispatch import TOOL_DEFINITIONS
        defn = next(t for t in TOOL_DEFINITIONS if t["name"] == "f3e_brand_voice_check")
        assert "description" in defn, "Tool definition must have description"
        assert "input_schema" in defn, "Tool definition must have input_schema"
        schema = defn["input_schema"]
        assert "brand" in schema["properties"], "Tool schema must include 'brand' param"
        assert "copy" in schema["properties"], "Tool schema must include 'copy' param"
        assert "brand" in schema["required"], "brand must be required"
        assert "copy" in schema["required"], "copy must be required"

    def test_dispatch_returns_string_for_valid_input(self):
        from cora.tools.tool_dispatch import dispatch
        result = dispatch(
            "f3e_brand_voice_check",
            {"brand": "pure", "copy": "Real energy for real life."},
            slack_user_id="U_TEST",
            entity="F3E",
        )
        assert isinstance(result, str), "dispatch must return a string"
        assert len(result) > 0, "dispatch must return non-empty string"

    def test_dispatch_handles_missing_brand(self):
        from cora.tools.tool_dispatch import dispatch
        result = dispatch(
            "f3e_brand_voice_check",
            {"copy": "Some copy here."},
            slack_user_id="U_TEST",
            entity="F3E",
        )
        assert isinstance(result, str)
        assert "brand" in result.lower(), "Missing brand param must produce helpful error mentioning 'brand'"

    def test_dispatch_handles_missing_copy(self):
        from cora.tools.tool_dispatch import dispatch
        result = dispatch(
            "f3e_brand_voice_check",
            {"brand": "mood"},
            slack_user_id="U_TEST",
            entity="F3E",
        )
        assert isinstance(result, str)
        assert "copy" in result.lower(), "Missing copy param must produce helpful error mentioning 'copy'"

    def test_dispatch_sleep_issue_surfaces_critical(self):
        from cora.tools.tool_dispatch import dispatch
        result = dispatch(
            "f3e_brand_voice_check",
            {"brand": "mood", "copy": "The sleep support drink for professionals."},
            slack_user_id="U_TEST",
            entity="F3E",
        )
        assert "CRITICAL" in result, "Sleep positioning must surface CRITICAL in dispatch output"

    def test_dispatch_channel_brand_inference(self):
        """In #f3-pure-social context, dispatch with brand=pure should work correctly."""
        from cora.tools.tool_dispatch import dispatch
        result = dispatch(
            "f3e_brand_voice_check",
            {"brand": "pure", "copy": "Go beast mode with F3 Pure today."},
            slack_user_id="U_TEST",
            entity="F3E",
        )
        assert isinstance(result, str)
        assert "WARNING" in result or "CRITICAL" in result, (
            "Beast mode in Pure copy must surface at least a WARNING"
        )
