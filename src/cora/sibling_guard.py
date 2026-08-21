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
    r"\b(COPA|BHRF|UnitedHealthcare|United\s+Health(?:care)?)\b",
    re.IGNORECASE,
)


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
    # SCOPE + ORDERING (cq-12bd309c93a8, live 2026-08-19 13:14:38). The block was
    # gated on entity == "LEX-LBHS", so in #llc-leadership (entity LEX-LLC) an ask
    # naming the LBHS COPA transcripts skipped it entirely and fell through to the
    # LBHS sibling redirect below: "That's Lexington Behavioral Health Services
    # information -- ask in an #lbhs-* channel." Those transcripts were PURGED on
    # 2026-07-21 and carry a permanent title-level ingest exclusion
    # (kb_exclusions.is_copa_meeting_title), so that sentence pointed a person at a
    # channel where the material does not exist and never will. The block now
    # covers the whole LEX family, GM level included, and because it is the FIRST
    # statement in this function it wins the ordering against every sibling
    # redirect by construction.
    #
    # Deliberately NOT portfolio-wide. What this closes is a MISLEADING POINTER,
    # not a leak -- the content is gone -- and the pointer is emitted by this
    # function, which only ever runs for LEX scope. A non-LEX channel naming
    # LBHS/COPA is redirected to LEX by cross_entity_guard and meets this block
    # there, so the chain still terminates honestly, without turning
    # "UnitedHealthcare" into a blocked word in eight unrelated entities.
    if _is_lex_scope(entity) and _LBHS_PRIVATE_RE.search(message):
        return (
            "That material is confidential to LBHS under NDA. I don't hold it in "
            "any channel and can't discuss it anywhere -- please contact LBHS "
            "leadership directly."
        )

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
