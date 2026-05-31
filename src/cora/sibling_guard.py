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
    if entity == "LEX-LBHS" and _LBHS_PRIVATE_RE.search(message):
        return (
            "That information is confidential to LBHS and cannot be discussed here. "
            "Please contact LBHS leadership directly."
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
