"""LEX sub-entity detection — shared by ingest-time tagging and the backfill script.

Tags a LEX knowledge chunk with a sub-entity (LEX-LLC / LEX-LTS / LEX-LBHS / LEX-LLA)
only when the text carries UNAMBIGUOUS signals — keywords that belong to exactly one
sub-entity. Chunks matching zero sub-entities are general LEX (GM-level) and stay
untagged; chunks matching two or more are ambiguous and also stay untagged.

This conservative rule is locked by the 2026-05-31 backfill ship (wishlists/lex.md):
- NULL sub_entity = GM-level / cross-sub-entity content, visible only in #lex-* channels
- Tagged chunks become visible in that sub-entity's channels (strict filter in
  store.build_sub_entity_filter excludes NULL from sub-entity channels)
- A wrong tag would expose a chunk to the wrong sub-entity audience, so detection
  must stay precision-first: when in doubt, stay NULL.

Used by:
- store.KnowledgeBase.upsert_documents — ingest-time tagging for every connector
  (drive_sweep, gmail, drive_asset, slack, fireflies, notion, asana, static_md)
- scripts/backfill_lex_sub_entity.py — catch-up sweep over existing NULL chunks
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Keyword patterns per sub-entity.
# Each entry is (sub_entity, [list of regex patterns]).
# A chunk is tagged only if it matches patterns for EXACTLY ONE sub-entity.
# If it matches patterns for 2+ sub-entities it stays NULL (ambiguous).
# ---------------------------------------------------------------------------
SUB_ENTITY_PATTERNS: list[tuple[str, list[str]]] = [
    (
        "LEX-LLC",
        [
            r"\[LEX-LLC\]",
            r"\bLexington LLC\b",
            r"\bDay Program\b",
            r"\bSupported Living\b",
            r"\bHCBS\b",
            r"\bJeff Montgomery\b",
            r"\bAaron Ferrucci\b",
            r"Lexington.*LLC",
        ],
    ),
    (
        "LEX-LTS",
        [
            r"\[LEX-LTS\]",
            r"\bLexington Therapeutic\b",
            r"\bProvider Type 15\b",
            r"\bDDD Therapy Revalidation\b",
            r"\bJustin Gilmore\b",
        ],
    ),
    (
        "LEX-LBHS",
        [
            r"\bLBHS\b",
            r"\[LEX-LBHS\]",
            r"\bBehavioral Health Services\b",
            r"\bCOPA\b",
            r"\bBHRF\b",
            r"\bJared Harker\b",
        ],
    ),
    (
        "LEX-LLA",
        [
            r"\[LEX-LLA\]",
            r"\bLex Life Academy\b",
            r"\bSandy Patel\b",
        ],
    ),
]

# Compile all patterns once at import
COMPILED_PATTERNS: list[tuple[str, list[re.Pattern]]] = [
    (se, [re.compile(p, re.IGNORECASE) for p in pats])
    for se, pats in SUB_ENTITY_PATTERNS
]


def detect_sub_entity(title: str, content: str) -> str | None:
    """Return the sub_entity if UNAMBIGUOUS, else None.

    None means either zero matches (general LEX, GM-level) or 2+ matches
    (ambiguous). Both stay untagged by design — see module docstring.
    """
    text = (title or "") + " " + (content or "")
    matched: set[str] = set()
    for sub_entity, patterns in COMPILED_PATTERNS:
        for pat in patterns:
            if pat.search(text):
                matched.add(sub_entity)
                break  # one match per sub-entity is enough
    if len(matched) == 1:
        return matched.pop()
    return None
