"""Deterministic sibling-entity redirect guard for LEX sub-entity channels.

When Cora operates in a LEX sub-entity channel (LEX-LLC, LEX-LTS, LEX-LBHS,
LEX-LLA) and a user message asks about a sibling entity, this guard intercepts
BEFORE the LLM call and returns the correct one-sentence redirect directly.

Why code-level instead of prompt-only:
    The model's helpfulness bias repeatedly overrides format instructions in
    the system prompt. Even with explicit "one sentence only / do not elaborate"
    instructions, the model adds context, offers alternatives, and references
    data it should not surface. A deterministic pre-LLM guard makes the redirect
    immune to LLM inference errors.

The guard uses word-boundary regex matching to avoid false positives (e.g.
"VILLA" should not trigger the LLA redirect).
"""

import re
from dataclasses import dataclass

from . import guard_input


@dataclass(frozen=True)
class _SiblingDef:
    entity_name: str          # Full name used in the redirect sentence
    channel_code: str         # Channel prefix for the #code-* reference
    patterns: tuple[str, ...] # Regex patterns (already compiled at module load)


# Map from the channel's entity → list of siblings that should trigger a redirect.
# Keywords are matched case-insensitively with word boundaries where applicable.
_SIBLING_DEFS: dict[str, list[_SiblingDef]] = {
    "LEX-LLC": [
        _SiblingDef(
            "Lex Life Academy", "lla",
            (r"\bLLA\b", r"\bLEX\s+LIFE\s+ACADEMY\b", r"\bLEX-LLA\b"),
        ),
        _SiblingDef(
            "Lexington Behavioral Health Services", "lbhs",
            (r"\bLBHS\b", r"\bLEXINGTON\s+BEHAVIORAL\b", r"\bBEHAVIORAL\s+HEALTH\b"),
        ),
        _SiblingDef(
            "Lexington Therapies", "lts",
            (r"\bLTS\b", r"\bLEXINGTON\s+THERAPIES\b"),
        ),
    ],
    "LEX-LTS": [
        _SiblingDef(
            "Lex Life Academy", "lla",
            (r"\bLLA\b", r"\bLEX\s+LIFE\s+ACADEMY\b", r"\bLEX-LLA\b"),
        ),
        _SiblingDef(
            "Lexington Behavioral Health Services", "lbhs",
            (r"\bLBHS\b", r"\bLEXINGTON\s+BEHAVIORAL\b", r"\bBEHAVIORAL\s+HEALTH\b"),
        ),
        _SiblingDef(
            "Lexington LLC", "llc",
            (r"\bLEXINGTON\s+LLC\b",),  # "LLC" alone is too broad; require "Lexington LLC"
        ),
    ],
    "LEX-LBHS": [
        _SiblingDef(
            "Lex Life Academy", "lla",
            (r"\bLLA\b", r"\bLEX\s+LIFE\s+ACADEMY\b", r"\bLEX-LLA\b"),
        ),
        _SiblingDef(
            "Lexington Therapies", "lts",
            (r"\bLTS\b", r"\bLEXINGTON\s+THERAPIES\b"),
        ),
        _SiblingDef(
            "Lexington LLC", "llc",
            (r"\bLEXINGTON\s+LLC\b",),
        ),
    ],
    "LEX-LLA": [
        _SiblingDef(
            "Lexington Behavioral Health Services", "lbhs",
            (r"\bLBHS\b", r"\bLEXINGTON\s+BEHAVIORAL\b", r"\bBEHAVIORAL\s+HEALTH\b"),
        ),
        _SiblingDef(
            "Lexington Therapies", "lts",
            (r"\bLTS\b", r"\bLEXINGTON\s+THERAPIES\b"),
        ),
        _SiblingDef(
            "Lexington LLC", "llc",
            (r"\bLEXINGTON\s+LLC\b",),
        ),
    ],
}

_SELF_NAMES: dict[str, str] = {
    "LEX-LLC":  "Lexington LLC",
    "LEX-LTS":  "Lexington Therapies",
    "LEX-LBHS": "Lexington Behavioral Health Services",
    "LEX-LLA":  "Lex Life Academy",
}

# Pre-compile all patterns at import time.
_COMPILED: dict[str, list[tuple[list[re.Pattern], _SiblingDef]]] = {}
for _entity, _siblings in _SIBLING_DEFS.items():
    _COMPILED[_entity] = [
        ([re.compile(p, re.IGNORECASE) for p in sib.patterns], sib)
        for sib in _siblings
    ]

# LBHS confidential-entity guard — terms that must never be discussed in any channel.
# lbhs.md explicitly forbids surfacing COPA/BHRF/UnitedHealthcare data; this enforces
# it pre-LLM so the model's helpfulness bias cannot override it.
_LBHS_PRIVATE_RE = re.compile(
    r"\b(COPA|BHRF|UHC|UnitedHealthcare|United\s+Health(?:care)?)\b",
    re.IGNORECASE,
)


# THE FAMILY-WIDE SET IS COPA ALONE (D-051 lens-4 HIGH, 2026-08-20).
#
# The first cut of the cq-12bd309c93a8 fix widened this WHOLE term list from
# LEX-LBHS to every LEX scope. Measured against the live KB, that was wrong in
# the direction that matters -- the other terms are ordinary LEX vocabulary, not
# NDA codenames:
#
#   * BHRF is an AHCCCS/ADHS licensure category. A KB search in LEX scope for
#     "BHRF continued-stay documentation" returns DDD Medical Policy Manual
#     chapters 300 / 320-V / 800 -- manuals ingested ON PURPOSE under D-046 so
#     Shaun's team could get policy answers. Blocking the term in #llc-* /
#     #lts-* / #lex-* refuses the exact question that corpus exists to answer.
#   * UnitedHealthcare is Lexington's OWN group-health carrier (KB entity=LEX,
#     drive_sweep 2026-08-17: premium invoices, eligibility, grace periods).
#     "When is the UnitedHealthcare invoice due?" is an AP question.
#
# And the widened refusal asserted "I don't hold it in any channel", which is
# FALSE for both -- thousands of chunks say otherwise. Only the purged COPA
# transcripts make that sentence true. A guard that states a false fact is worse
# than one that points at the wrong channel, which is what the cq was about.
#
# So: COPA hard-blocks across the LEX family with a sentence that is TRUE of it;
# BHRF / UHC / UnitedHealthcare keep their pre-existing LEX-LBHS-only scope and
# their pre-existing wording. UHC (the abbreviation actually used in the LEX
# billing corpus) joins the LBHS-scoped set, where it belongs.
_COPA_NDA_RE = re.compile(r"\bCOPA\b", re.IGNORECASE)


def _is_lex_scope(entity: str) -> bool:
    """LEX GM-level or any LEX sub-entity."""
    e = (entity or "").strip().upper()
    return e == "LEX" or e.startswith("LEX-")


def check_redirect(entity: str, message: str) -> str | None:
    """Return a one-sentence redirect if message asks about a sibling entity.

    Returns None for non-LEX-sub-entity channels and when no sibling keyword
    is detected. When a match fires, the returned string is the COMPLETE
    response — callers must post it as-is with no additions.

    Matching is case-insensitive with word boundaries to avoid false positives
    (e.g. "villa" should not match LLA, "Lexington" alone should not redirect).
    First-match-wins (highest specificity keywords listed first in _SIBLING_DEFS).
    """
    # LBHS confidential-entity guard: hard-block COPA/BHRF/UnitedHealthcare references
    # before any LLM call, regardless of how the question is phrased.
    # EVALUATED ON THE RAW MESSAGE, BEFORE any normalization -- a hard-block that
    # promises "regardless of how the question is phrased" must never read
    # rewritten text. With the strip first, "Reason: COPA diligence for
    # UnitedHealthcare" inside an inventory-shaped message bypassed it entirely
    # (D-051 HIGH, 2026-08-17). An F3E inventory write never legitimately names
    # COPA/BHRF/UHC, so there is no false-positive cost to checking raw.
    #
    # COPA IS CHECKED FIRST, including inside LEX-LBHS. Both patterns match a
    # COPA ask there, and the LBHS wording ("cannot be discussed here") is the
    # misleading half for this content class -- "here" is what sent someone
    # looking elsewhere in the first place. The accurate sentence has to win
    # wherever both apply.
    if _is_lex_scope(entity) and _COPA_NDA_RE.search(message):
        return (
            "The COPA diligence material is confidential to LBHS under NDA and "
            "was removed from my knowledge base -- I don't hold it in any "
            "channel, so there is nowhere I can answer that. Please contact "
            "LBHS leadership directly."
        )

    if entity == "LEX-LBHS" and _LBHS_PRIVATE_RE.search(message):
        return (
            "That information is confidential to LBHS and cannot be discussed here. "
            "Please contact LBHS leadership directly."
        )
    # COPA, across the whole LEX family (cq-12bd309c93a8, live 2026-08-19
    # 13:14:38). The block was gated on entity == "LEX-LBHS", so in
    # #llc-leadership (entity LEX-LLC) an ask naming the LBHS COPA transcripts
    # skipped it entirely and fell through to the LBHS sibling redirect below:
    # "That's Lexington Behavioral Health Services information -- ask in an
    # #lbhs-* channel." Those transcripts were PURGED on 2026-07-21 and carry a
    # permanent title-level ingest exclusion (kb_exclusions.is_copa_meeting_title),
    # so that sentence pointed a person at a channel where the material does not
    # exist and never will. Because this runs BEFORE the sibling loop it wins the
    # ordering by construction.
    #
    # NOT portfolio-wide, and the honest reason is not the one the first cut gave.
    # That comment claimed a non-LEX channel "is redirected to LEX by
    # cross_entity_guard and meets this block there" -- it does not:
    # cross_entity_guard returns its own COMPLETE response and app.py posts it and
    # RETURNS, so this function is never re-entered with entity="LEX" (D-051
    # lens-5, measured). The real reason to stop here is narrower and sufficient:
    # what this closes is a MISLEADING POINTER, not a leak -- the content is
    # purged, so a non-LEX channel that reaches the model finds nothing and says
    # so, which is already honest. The residual is that cross_entity_guard's own
    # "ask in #lex-leadership" pointer has the same shape for an ask that names
    # Lexington; that is its defect to fix, in its own text, not something this
    # guard can reach from here.
    compiled_siblings = _COMPILED.get(entity)
    if not compiled_siblings:
        return None

    self_name = _SELF_NAMES[entity]
    for patterns, sib in compiled_siblings:
        for pat in patterns:
            if pat.search(message):
                return (
                    f"That's {sib.entity_name} information — "
                    f"ask in an #{sib.channel_code}-* channel. "
                    f"I'm scoped to {self_name} here."
                )

    return None
