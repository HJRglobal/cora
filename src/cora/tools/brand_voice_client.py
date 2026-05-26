"""Brand voice checker for F3 Energy sub-brands (Pure, Mood, Energy).

Given a draft piece of copy and a target brand, checks against brand-guidelines V1
locked specs and returns a structured findings report for Claude to synthesize.

Analysis checks:
  1. Sleep positioning (Mood ONLY — CRITICAL anti-pattern, non-negotiable)
  2. Sibling-brand drift — language that belongs to a different F3 sub-brand's lane
  3. Anti-positioning — explicit competitor refs, cross-entity violations, banned framings
  4. Health / nutrient claims — universal guardrail across all three brands
  5. Voice spec notes — brief summary of the brand's locked pillars for Claude's context

All rules are embedded from brand-guidelines V1 (locked 2026-05-22).
No external API calls — deterministic pattern matching, instant results.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger(__name__)

# Type aliases
Severity = Literal["CRITICAL", "WARNING", "INFO"]
BrandCode = Literal["pure", "mood", "energy"]

VALID_BRANDS: tuple[str, ...] = ("pure", "mood", "energy")


# ─── Data structures ──────────────────────────────────────────────────────────


@dataclass
class Finding:
    severity: Severity
    category: str
    term_found: str
    message: str


@dataclass
class BrandCheckResult:
    brand: str
    copy_preview: str
    findings: list[Finding] = field(default_factory=list)
    voice_notes: str = ""

    @property
    def has_critical(self) -> bool:
        return any(f.severity == "CRITICAL" for f in self.findings)

    @property
    def has_warning(self) -> bool:
        return any(f.severity == "WARNING" for f in self.findings)

    @property
    def verdict(self) -> str:
        if self.has_critical:
            return "NEEDS REVISION — critical issues found, must fix before publishing"
        if self.has_warning:
            return "REVIEW BEFORE POSTING — warnings found, consider revising"
        return "PASSES pattern check — human review recommended for tone"


# ─── Universal: health / nutrient claim patterns ──────────────────────────────
#
# Applies to ALL three brands. Functional ingredients = FDA scrutiny.
# Any claim linking a product to treating/preventing/curing a disease or
# condition must go to Harrison → Emily Stubbs (Visibility legal) before publish.
# Only use claims that appear verbatim on the NSF-certified can label.

_HEALTH_CLAIM_PATTERNS: list[tuple[str, str]] = [
    (
        r"\b(cures?|treats?|prevents?|heals?)\b.{0,50}\b(disease|condition|disorder|illness|symptom)",
        "Therapeutic claim (cure/treat/prevent/heal)",
    ),
    (
        r"\b(reduces?|lowers?|eliminates?|relieves?)\b.{0,50}\b(anxiety|depression|stress disorder|chronic pain|inflammation)\b",
        "Symptom-reduction claim",
    ),
    (
        r"\bclinically\s+(proven|tested|studied|validated|shown)\b",
        "Clinical efficacy claim ('clinically proven/tested/studied')",
    ),
    (
        r"\bFDA[- ]?(approved|cleared|certified|registered)\b",
        "FDA claim",
    ),
    (
        r"\b(diagnose|diagnoses|diagnosing)\b",
        "Diagnostic language",
    ),
    (
        r"\b(boosts?|improves?|strengthens?|supports?)\b.{0,30}\b(immune|immunity)\b",
        "Immune-system claim",
    ),
    (
        r"\bhealth\s+(claim|benefit|statement)\b",
        "Explicit 'health benefit/claim' language",
    ),
    (
        r"\b(improves?|enhances?)\s+(cognitive\s+function|brain\s+function|mental\s+performance)\b",
        "Cognitive function claim",
    ),
    (
        r"\bNSF[- ]?certified\b",
        "NSF certification claim — only use if this exact language is on the can label; verify before publishing",
    ),
]


def _check_health_claims(copy: str) -> list[Finding]:
    findings: list[Finding] = []
    for pattern, label in _HEALTH_CLAIM_PATTERNS:
        if re.search(pattern, copy, re.IGNORECASE):
            findings.append(
                Finding(
                    severity="CRITICAL",
                    category="Health/nutrition claim",
                    term_found=label,
                    message=(
                        f"Health or nutrient claim detected: {label}. "
                        "Route to Harrison → Emily Stubbs (Visibility legal) before publishing. "
                        "Only use claims that appear verbatim on the NSF-certified can label."
                    ),
                )
            )
    return findings


# ─── Cross-entity: UFL pause + HJRP-RR venue boundary (all brands) ──────────
#
# UFL: F3-UFL crossover content is blocked per 2026-05-10 directive.
# HJRP-RR: Rogers Ranch (wedding venue / corporate retreat / vacation rental)
#   is a separate HJR Properties sub-entity. F3 Energy brand alignment with
#   venue/wedding/retreat content is BLOCKED — redirect to HJRP-RR.

_CROSS_ENTITY_BANNED: list[tuple[str, str]] = [
    # UFL pause (2026-05-10)
    (
        "ufl",
        "Cross-entity: F3-UFL crossover content is BLOCKED per 2026-05-10 pause directive. "
        "No F3-UFL athlete partnerships or joint content until portfolio is profitable.",
    ),
    (
        "united fight league",
        "Cross-entity: F3-UFL crossover content is BLOCKED per 2026-05-10 pause directive.",
    ),
    # HJRP-RR venue boundary
    (
        "wedding venue",
        "Cross-entity: wedding venue content is Rogers Ranch (HJRP-RR) territory, not F3 Energy. "
        "Do not position F3 as a venue partner or wedding drink. Route to #rogers-ranch or #hjrp-leadership.",
    ),
    (
        "corporate retreat",
        "Cross-entity: corporate retreat positioning is Rogers Ranch (HJRP-RR) territory, not F3 Energy. "
        "Retreat event sponsorships belong to HJRP-RR — redirect there.",
    ),
    (
        "ranch retreat",
        "Cross-entity: ranch retreat content is Rogers Ranch (HJRP-RR) territory, not F3 Energy.",
    ),
    (
        "wedding day",
        "Cross-entity: wedding-day framing positions F3 as a wedding/venue product — "
        "that's Rogers Ranch (HJRP-RR) territory. Do not align F3 brand with wedding event copy.",
    ),
    (
        "venue sponsor",
        "Cross-entity: venue sponsorship is Rogers Ranch (HJRP-RR) territory, not F3 Energy. "
        "Retail activations and gym partnerships are fine; venue-business sponsorships are not.",
    ),
]


# ─── F3 Pure — brand rules ────────────────────────────────────────────────────
#
# Avatar: "Lauren" (25-35, Pilates-mom / Sprouts-regular)
# Tagline: "Real energy for real life."
# Palette: Pure Teal #2EBFB3 / Pure Coral #F47B6C / Pure Green #7BC67E on Pure White #FAFAF7
# Typography: Josefin Sans Thin/ExtraLight + Nunito Sans Regular
# Pure is the NATURAL CHANNEL — NOT gym/MMA, NOT calming/anxiety-relief.

# Drift toward Energy's lane (gym/MMA performance framing)
_PURE_ENERGY_DRIFT: list[tuple[str, str]] = [
    ("pre-workout", "Energy lane: 'pre-workout' is F3 Energy's gym-performance positioning — not Pure's natural channel"),
    ("pre workout", "Energy lane: 'pre workout' is F3 Energy's gym-performance positioning — not Pure's natural channel"),
    ("beast mode", "Energy lane: 'beast mode' belongs to F3 Energy's intensity framing"),
    ("beast", "Energy lane: 'beast' identity belongs to F3 Energy / MMA, not Pure's Pilates-adjacent lane"),
    ("gains", "Energy lane: body-composition 'gains' language is gym territory, not Pure's lifestyle positioning"),
    ("pump", "Energy lane: 'pump' is gym/pre-workout vocabulary — doesn't fit Pure's natural channel"),
    ("shredded", "Energy lane: body-composition language doesn't fit Pure's natural, everyday positioning"),
    ("bulk", "Energy lane: bulking/cutting language is gym territory — belongs to Energy, not Pure"),
    ("knockout", "Energy lane: MMA/fight framing belongs to F3 Energy (Alex Cordova sub-brand)"),
    ("mma", "Energy lane: MMA is F3 Energy's Alex Cordova sub-brand territory — not Pure"),
    ("cage", "Energy lane: cage/fight language belongs to F3 Energy, not Pure"),
    ("gym rat", "Energy lane: 'gym rat' identity doesn't fit Pure's Pilates/Sprouts-adjacent avatar (Lauren)"),
    ("fight night", "Energy lane: fight-night framing is F3 Energy territory"),
]

# Drift toward Mood's lane (calming/anxiety-relief framing)
_PURE_MOOD_DRIFT: list[tuple[str, str]] = [
    ("calm the noise", "Mood lane: 'Calm the Noise' is Mood's exact tagline — never use in Pure copy"),
    ("calm your", "Mood lane: 'calm your [X]' framing is Mood's territory, not Pure's"),
    ("calming", "Mood lane: calming claims belong to F3 Mood (chamomile/GABA positioning)"),
    ("anxiety", "Mood lane: anxiety relief is explicitly Mood's positioning — not Pure's"),
    ("stress relief", "Mood lane: stress-relief claims belong to F3 Mood"),
    ("de-stress", "Mood lane: de-stressing language belongs to Mood, not Pure"),
    ("relax", "Mood lane: relaxation framing belongs to F3 Mood — Pure is energizing, not relaxing"),
    ("wind down", "Mood lane: 'wind down' positions toward Mood's end-of-shift recovery territory"),
    ("decompress", "Mood lane: decompressing = Mood's professional-recovery positioning"),
    ("take the edge off", "Mood lane: 'take the edge off' is Mood's functional-relief lane"),
]

_PURE_VOICE_PILLARS = (
    'Avatar: "Lauren" (25-35, Pilates-mom / Sprouts-regular). '
    'Tagline: "Real energy for real life." '
    "Palette: Pure Teal #2EBFB3 / Pure Coral #F47B6C / Pure Green #7BC67E on Pure White #FAFAF7. "
    "Typography: Josefin Sans Thin/ExtraLight (headlines) + Nunito Sans Regular (body). "
    "Voice: Clean, warm, conversational. Never clinical, never gym-bro. "
    "Photography: bright natural light, outdoor movement, farmers-market/pilates aesthetic. "
    "Pure is the NATURAL CHANNEL — everyday approachable energy. "
    "Anti-patterns: gym/MMA/fight language (Energy's lane), calming/anxiety-relief claims (Mood's lane), "
    "false natural/organic claims without substantiation."
)


# ─── F3 Mood — brand rules ────────────────────────────────────────────────────
#
# Avatar: "Marcus" (35-50, ER doctor / trial attorney / first responder)
# Tagline: "Calm the Noise.™"
# Palette: Mood Black #1A1A1A + Mood Gold #C9A84C
# Typography: Josefin Sans Bold (headlines) + Nunito Sans Regular (body)
# CRITICAL RULE: Mood is NOT a sleep drink. NOT a sleep aid. NEVER position as sedating.
# Ingredients: chamomile, GABA, magnesium, valerian root — framed as CLARITY/FOCUS, not sedation.

# Sleep-adjacent language — CRITICAL severity (non-negotiable anti-positioning)
_MOOD_SLEEP_BANNED: list[tuple[str, str]] = [
    (
        "sleep support",
        "CRITICAL — sleep supplement category. Mood is NOT a sleep drink. Never use 'sleep support'.",
    ),
    (
        "sleep aid",
        "CRITICAL — Mood is explicitly NOT a sleep aid. Remove immediately.",
    ),
    (
        "helps you sleep",
        "CRITICAL — Mood must not be positioned as helping users sleep.",
    ),
    (
        "helps with sleep",
        "CRITICAL — Mood must not be positioned as helping users sleep.",
    ),
    (
        "better sleep",
        "CRITICAL — 'better sleep' = sleep-supplement positioning. Mood is NOT a sleep drink.",
    ),
    (
        "melatonin",
        "CRITICAL — Melatonin is NOT in Mood's formula. Mentioning it implies sleep-aid positioning. Remove.",
    ),
    (
        "bedtime",
        "CRITICAL — Bedtime framing positions Mood as a sleep product. Mood is NOT a sleep drink.",
    ),
    (
        "night cap",
        "CRITICAL — 'Night cap' = pre-sleep drink. Mood is NOT a sleep drink.",
    ),
    (
        "nightcap",
        "CRITICAL — 'Nightcap' = pre-sleep drink. Mood is NOT a sleep drink.",
    ),
    (
        "wind down for bed",
        "CRITICAL — 'Wind down for bed' is sleep-aid framing. Mood is NOT a sleep drink.",
    ),
    (
        "before bed",
        "CRITICAL — 'Before bed' implies sleep positioning. Mood is NOT a sleep drink. "
        "Mood is for high-stakes hours, not pre-sleep.",
    ),
    (
        "sleep tight",
        "CRITICAL — Sleep farewell framing implies Mood helps with sleep. Remove.",
    ),
    (
        "goodnight",
        "CRITICAL — 'Goodnight' = nighttime/sleep positioning. Mood is NOT a sleep drink.",
    ),
    (
        "drowsy",
        "CRITICAL — Mood must never imply drowsiness. Functional ingredients are framed as focus/clarity, not sedation.",
    ),
    (
        "sleepy",
        "CRITICAL — Mood must never imply drowsiness. Remove sleepy/sedating language.",
    ),
    (
        "sleep",
        "CRITICAL (check context) — Mood is NOT a sleep drink. If 'sleep' appears in a testimonial setup "
        "('I couldn't sleep before my trial — I tried Mood'), that may be borderline acceptable, "
        "but all sleep-adjacent language is high risk for Mood's brand positioning. Consider removing.",
    ),
]

# Drift toward Energy's lane
_MOOD_ENERGY_DRIFT: list[tuple[str, str]] = [
    ("pre-workout", "Energy lane: Mood is not a pre-workout drink — performance framing belongs to F3 Energy"),
    ("pre workout", "Energy lane: Mood is not a pre-workout drink"),
    ("beast mode", "Energy lane: 'beast mode' belongs to F3 Energy's intensity framing, not Mood's professional lane"),
    ("gains", "Energy lane: gym body-comp language doesn't fit Mood's executive/professional avatar (Marcus)"),
    ("pump", "Energy lane: 'pump' is gym vocabulary — doesn't fit Mood's professional positioning"),
    ("mma", "Energy lane: MMA belongs to F3 Energy's Alex Cordova sub-brand"),
    ("cage", "Energy lane: cage/fight framing belongs to F3 Energy"),
    ("knockout", "Energy lane: knockout/fight framing belongs to F3 Energy"),
    ("beast", "Energy lane: 'beast' identity belongs to Energy, not Mood's understated professional voice"),
]

# Drift toward Pure's lane
_MOOD_PURE_DRIFT: list[tuple[str, str]] = [
    (
        "clean energy for everyone",
        "Pure lane: 'clean energy for everyone' is Pure's accessible-to-all positioning — Mood is narrower/professional",
    ),
    (
        "all natural",
        "Pure lane: 'all natural' claims edge toward Pure's natural-channel positioning; needs substantiation regardless",
    ),
    (
        "organic",
        "Pure lane: organic framing belongs to Pure's natural channel — and requires factual substantiation",
    ),
    (
        "no artificial",
        "Pure lane: 'no artificial X' language belongs to Pure's clean-ingredient positioning, not Mood's",
    ),
    (
        "for the whole family",
        "Pure lane: family-friendly framing belongs to Pure (Lauren), not Mood's professional/high-stakes audience (Marcus)",
    ),
]

_MOOD_VOICE_PILLARS = (
    'Avatar: "Marcus" (35-50, ER doctor / trial attorney / first responder). '
    'Tagline: "Calm the Noise.™" '
    "Palette: Mood Black #1A1A1A + Mood Gold #C9A84C. "
    "Typography: Josefin Sans Bold (headlines) + Nunito Sans Regular (body). "
    "Voice: Confident, understated, intelligent. Not bubbly. Not bro-y. Earned calm. "
    "Mood is functional focus + recovery during HIGH-STAKES HOURS — end-of-shift reset, not sleep. "
    "Ingredients: chamomile, GABA, magnesium, valerian root — framed as CLARITY/FOCUS, NOT sedation. "
    "CRITICAL RULE: Mood is NOT a sleep drink. NOT a sleep aid. NEVER position as sedating or pre-sleep. "
    "Anti-patterns: any sleep language (CRITICAL), gym/MMA language (Energy's lane), "
    "natural/clean claims (Pure's lane)."
)


# ─── F3 Energy — brand rules ─────────────────────────────────────────────────
#
# Avatar: "Alex" (22-42, MMA-adjacent; named for Alex Cordova)
# Taglines: "Fuel. Focus. Finish." / "When Clarity Counts."
# Palette: Energy Red #B02225 + Energy Bright Red #ED1C24
# Photography: red duotone signature
# Typography: Josefin Sans ExtraBold/Black (headlines) + Nunito Sans SemiBold (body)
# Ingredients: ginseng panax, BCAA, L-theanine, ginkgo biloba (nootropic framing)
# Alex sub-account voice: MMA athlete — direct, performance-focused, purposeful.
# NOT generic gym-bro. NOT bro-y or flashy. NOT calming.

# Drift toward Mood's lane (calming/anxiety-relief framing)
_ENERGY_MOOD_DRIFT: list[tuple[str, str]] = [
    ("calming", "Mood lane: calming language belongs to F3 Mood — Energy is performance-driven, not calming"),
    ("calm the noise", "Mood lane: 'Calm the Noise' is Mood's trademarked tagline — never use in Energy copy"),
    ("calm your", "Mood lane: 'calm your [X]' is Mood's territory"),
    ("anxiety relief", "Mood lane: anxiety relief is explicitly Mood's positioning"),
    ("stress relief", "Mood lane: stress relief belongs to Mood — Energy is about performance focus, not stress reduction"),
    ("decompress", "Mood lane: decompressing/recovering from stress = Mood's end-of-shift territory"),
    ("wind down", "Mood lane: wind-down language belongs to Mood — Energy is about firing up, not winding down"),
    ("take the edge off", "Mood lane: 'take the edge off' = Mood's functional-relief lane"),
]

# Drift toward Pure's lane (over-softening Energy's intensity)
_ENERGY_PURE_DRIFT: list[tuple[str, str]] = [
    (
        "gentle energy",
        "Pure lane: 'gentle energy' over-softens Energy's direct/intense brand — Energy is purposeful and intense, not gentle",
    ),
    (
        "clean energy for everyone",
        "Pure lane: broad accessibility framing is Pure's natural-channel positioning",
    ),
    (
        "for the whole family",
        "Pure lane: family-friendly framing belongs to Pure (Lauren) — Energy is Alex/MMA-adjacent",
    ),
    (
        "natural choice",
        "Pure lane: 'natural choice' language is Pure's lane — Energy doesn't lead with natural positioning",
    ),
    (
        "light energy",
        "Pure lane: 'light energy' is too soft for Energy's direct, performance-focused brand voice",
    ),
    (
        "delicate",
        "Pure lane: 'delicate' is over-softened for Energy — doesn't fit the MMA-adjacent voice",
    ),
]

# Anti-positioning: competitors, cross-entity, generic bro language
_ENERGY_ANTI_POSITIONING: list[tuple[str, str]] = [
    ("red bull", "Competitor: Don't name or compare to competitor brands in copy"),
    ("redbull", "Competitor: Don't name or compare to competitor brands in copy"),
    ("monster energy", "Competitor: Don't name or compare to competitor brands in copy"),
    ("bang energy", "Competitor: Don't name or compare to competitor brands in copy"),
    ("celsius", "Competitor: Don't name or compare to competitor brands in copy"),
    ("5-hour energy", "Competitor: Don't name or compare to competitor brands in copy"),
    (
        "ufc",
        "Competitor: UFC is a competitor brand entity — don't mention in F3 Energy copy. "
        "MMA is fine; UFC specifically is competitor territory.",
    ),
    (
        "bro",
        "Voice: 'bro' language makes Energy generic gym-bro — Alex is MMA-adjacent, purposeful, not generic bro-y",
    ),
    (
        "swole",
        "Voice: bodybuilder vocabulary doesn't fit Energy's focused/purposeful MMA-adjacent identity",
    ),
    (
        "shredded",
        "Voice: body-composition language doesn't fit Energy's focused/purposeful brand voice — "
        "Energy is about performance and mental clarity, not physique goals",
    ),
]

_ENERGY_VOICE_PILLARS = (
    'Avatar: "Alex" (22-42, MMA-adjacent; named after Alex Cordova). '
    'Taglines: "Fuel. Focus. Finish." and "When Clarity Counts." '
    "Palette: Energy Red #B02225 + Energy Bright Red #ED1C24. "
    "Photography: red duotone signature. "
    "Typography: Josefin Sans ExtraBold/Black (headlines) + Nunito Sans SemiBold (body). "
    "Ingredients: ginseng panax, BCAA, L-theanine, ginkgo biloba — framed as nootropic performance. "
    "Voice: Direct, intense, purposeful. NOT generic gym-bro. NOT flashy. NOT calming. "
    "Alex Cordova sub-account: MMA athlete voice — MMA-adjacent is fine; UFC brand name is not. "
    "Anti-patterns: competitor brand names (Red Bull, Monster, UFC), generic bro/swole vocabulary, "
    "calming/anxiety language (Mood's lane), gentle/natural framing (Pure's lane)."
)


# ─── Core check logic ─────────────────────────────────────────────────────────


def _check_patterns(
    copy_lower: str,
    patterns: list[tuple[str, str]],
    severity: Severity,
    category: str,
) -> list[Finding]:
    """Substring-match a list of (term, message) patterns against lowercased copy."""
    findings: list[Finding] = []
    for term, message in patterns:
        if term.lower() in copy_lower:
            findings.append(
                Finding(
                    severity=severity,
                    category=category,
                    term_found=term,
                    message=message,
                )
            )
    return findings


def check_copy(brand: str, copy: str) -> BrandCheckResult:
    """Check copy against brand-guidelines V1 rules for the specified F3 sub-brand.

    Args:
        brand: 'pure', 'mood', or 'energy' (case-insensitive).
        copy:  The draft text to analyse.

    Returns:
        A BrandCheckResult with ordered findings and a voice-pillar summary.
    """
    brand_norm = brand.strip().lower()
    copy_preview = (copy[:140] + "...") if len(copy) > 140 else copy
    result = BrandCheckResult(brand=brand_norm, copy_preview=copy_preview)

    if brand_norm not in VALID_BRANDS:
        result.findings.append(
            Finding(
                severity="CRITICAL",
                category="Tool error",
                term_found=repr(brand),
                message=(
                    f"Unknown brand {brand!r}. Must be one of: pure, mood, energy. "
                    "Ask the user which F3 sub-brand this copy is for."
                ),
            )
        )
        return result

    copy_lower = copy.lower()

    # ── Universal checks ────────────────────────────────────────────────────
    # Health/nutrition claims — applies to all brands
    result.findings.extend(_check_health_claims(copy))

    # Cross-entity UFL pause — applies to all brands
    result.findings.extend(
        _check_patterns(copy_lower, _CROSS_ENTITY_BANNED, "CRITICAL", "Cross-entity (UFL pause)")
    )

    # ── Brand-specific checks ───────────────────────────────────────────────
    if brand_norm == "pure":
        result.findings.extend(
            _check_patterns(copy_lower, _PURE_ENERGY_DRIFT, "WARNING", "Energy-lane drift")
        )
        result.findings.extend(
            _check_patterns(copy_lower, _PURE_MOOD_DRIFT, "WARNING", "Mood-lane drift")
        )
        result.voice_notes = _PURE_VOICE_PILLARS

    elif brand_norm == "mood":
        # Sleep positioning is CRITICAL for Mood — check first
        result.findings.extend(
            _check_patterns(copy_lower, _MOOD_SLEEP_BANNED, "CRITICAL", "Sleep positioning (CRITICAL)")
        )
        result.findings.extend(
            _check_patterns(copy_lower, _MOOD_ENERGY_DRIFT, "WARNING", "Energy-lane drift")
        )
        result.findings.extend(
            _check_patterns(copy_lower, _MOOD_PURE_DRIFT, "WARNING", "Pure-lane drift")
        )
        result.voice_notes = _MOOD_VOICE_PILLARS

    elif brand_norm == "energy":
        result.findings.extend(
            _check_patterns(copy_lower, _ENERGY_MOOD_DRIFT, "WARNING", "Mood-lane drift")
        )
        result.findings.extend(
            _check_patterns(copy_lower, _ENERGY_PURE_DRIFT, "WARNING", "Pure-lane drift")
        )
        result.findings.extend(
            _check_patterns(copy_lower, _ENERGY_ANTI_POSITIONING, "CRITICAL", "Anti-positioning")
        )
        result.voice_notes = _ENERGY_VOICE_PILLARS

    return result


# ─── Output formatter ─────────────────────────────────────────────────────────


def format_result_for_llm(result: BrandCheckResult) -> str:
    """Format a BrandCheckResult as structured text for Claude to synthesize into a Slack reply.

    Claude should:
    - Present findings clearly and conversationally
    - Offer to help revise copy if issues are found
    - Apply additional tone reasoning beyond what pattern matching captures
    """
    brand_display = result.brand.upper() if result.brand in VALID_BRANDS else result.brand
    lines = [
        f"BRAND VOICE CHECK — F3 {brand_display}",
        f'Copy checked: "{result.copy_preview}"',
        "",
    ]

    if not result.findings:
        lines.extend(
            [
                "FINDINGS: None — no pattern-rule violations detected.",
                "",
                f"VERDICT: {result.verdict}",
                "",
                f"VOICE SPEC: {result.voice_notes}",
                "",
                "NOTE: Pattern matching catches explicit violations. "
                "Apply your own brand-context reasoning for subtle tone issues not caught here. "
                "Offer to help refine the copy further if asked.",
            ]
        )
        return "\n".join(lines)

    # Partition by severity
    criticals = [f for f in result.findings if f.severity == "CRITICAL"]
    warnings = [f for f in result.findings if f.severity == "WARNING"]
    infos = [f for f in result.findings if f.severity == "INFO"]

    total = len(result.findings)
    lines.append(f"FINDINGS ({total} issue{'s' if total != 1 else ''}):")
    lines.append("")

    if criticals:
        lines.append("🚨 CRITICAL — must fix before publishing:")
        for f in criticals:
            lines.append(f'  • [{f.category}] term="{f.term_found}" — {f.message}')
        lines.append("")

    if warnings:
        lines.append("⚠️ WARNING — review and consider revising:")
        for f in warnings:
            lines.append(f'  • [{f.category}] term="{f.term_found}" — {f.message}')
        lines.append("")

    if infos:
        lines.append("ℹ️ INFO:")
        for f in infos:
            lines.append(f'  • [{f.category}] term="{f.term_found}" — {f.message}')
        lines.append("")

    lines.extend(
        [
            f"VERDICT: {result.verdict}",
            "",
            f"VOICE SPEC: {result.voice_notes}",
            "",
            "NOTE: This tool checks explicit patterns only. "
            "Apply your own brand-context reasoning for nuanced tone issues not captured by pattern matching. "
            "If issues were found, offer to help revise the copy.",
        ]
    )

    return "\n".join(lines)
