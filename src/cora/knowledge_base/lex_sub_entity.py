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


# ---------------------------------------------------------------------------
# W6-01 (2026-07-05) — restricted-LEX ingest deny-list for NON-Slack sweep sources
# ---------------------------------------------------------------------------
# The Slack sweeps deny the lbhs*/lts* CHANNELS at the source (slack_sweep_policy +
# slack-sweep-policy.yaml). But gmail_reader + drive_sweep have no channel, so LBHS
# (42 CFR Part 2) and LTS (Provider-Type-15 therapy) content still reached the KB via
# those paths (audit: 585 drive_sweep + 536 gmail LBHS; 427 gmail + 237 drive_sweep LTS).
# A BAA does NOT waive 42 CFR Part 2, so this mirrors the Slack deny-list for non-Slack
# sources: any gmail/drive_sweep doc whose CONTENT resolves (detect_sub_entity) to a
# restricted sub-entity is dropped at the ingest choke point rather than stored.
#
# SCOPE (deliberately narrow — verify-first 2026-07-05):
#   - gmail + drive_sweep ONLY. Slack lbhs*/lts* channels are already denied upstream;
#     a content-tagged LBHS/LTS chunk arriving via a GM channel (#lex-leadership) is
#     GM-level leadership context and stays (subject to the retrieval scrub).
#   - Keys on the RESOLVED sub_entity tag (content-based detect_sub_entity), NOT a raw
#     domain-substring drop: 3,101 gmail/drive chunks merely MENTION a lbhs/lts domain,
#     mostly LBHS/LTS *business* (loans, management fees, PTO) that is NOT Part-2 clinical
#     — dropping all of those would be broad over-refusal beyond scope. The clinical
#     residue among the untagged is caught at EGRESS (context_loader._withhold_non_lex_phi,
#     W2-01) and monitored at rest (W6-06).
#
# W6-01 Fix-A (D-073, 2026-07-06): the tag+source is now only the SCOPE gate — it decides
# WHICH docs to PHI-check, not which to drop. Harrison directed that LBHS/LTS *business*
# (payroll / fees / PTO / aggregate "client billing" — NOT patient records, NOT 42-CFR-Part-2)
# be KEPT + retrievable, while only actual PHI is dropped. Verified against the live corpus:
# of 1,135 LBHS + 665 LTS tagged chunks, only ~42 carry PHI content; ~1,758 are business.
# So the DROP decision moved to the PHI-content predicate below.
RESTRICTED_INGEST_SUB_ENTITIES: tuple[str, ...] = ("LEX-LBHS", "LEX-LTS")
RESTRICTED_INGEST_SOURCES: tuple[str, ...] = ("gmail", "drive_sweep")


def is_restricted_lex_ingest(source: str | None, sub_entity: str | None) -> bool:
    """SCOPE gate (W6-01): is this a non-Slack sweep (gmail/drive_sweep) doc/chunk resolved
    to a restricted LEX sub-entity (LBHS/LTS)? True = it is a candidate for the PHI-content
    drop below (NOT, by itself, a drop — see restricted_lex_phi_content_drop). Slack
    lbhs*/lts* channels are denied upstream; GM-level / LLC / LLA / other sources are out of
    scope. Shared by upsert_documents + purge_lex_restricted_kb.py so scope can't diverge.
    """
    return (
        source in RESTRICTED_INGEST_SOURCES
        and sub_entity in RESTRICTED_INGEST_SUB_ENTITIES
    )


def restricted_lex_phi_content_drop(
    source: str | None,
    sub_entity: str | None,
    title: str | None,
    content: str | None,
    allowed_names: set[str] | None = None,
) -> bool:
    """W6-01 Fix-A (D-073): DROP a gmail/drive_sweep LBHS/LTS doc ONLY when its content
    actually carries PHI — NOT on the bare entity tag.

    True iff the doc is in scope (is_restricted_lex_ingest) AND its title+content trips the
    TAG-SCOPED PHI predicate phi_guard.non_lex_phi_backstop_trips_individual: clinical FRAMING
    (DOB / ICD-10 / "diagnosed with X"), or a bare dx/med term WITH a specific named individual,
    or named-individual program billing. NOT the LIVE variant's program-cue leg — on content
    already tagged LBHS/LTS the program cue is present by construction (the tag keyword IS the
    cue), so that leg would over-drop business docs mentioning a dx/med term with no patient
    (a school name / job title / fee schedule) [D-051 re-gate, D-073].

    LBHS/LTS BUSINESS (payroll / fees / PTO / aggregate "client billing" with no named individual,
    and business docs that merely mention a dx/med descriptor) is KEPT + retrievable. Deterministic,
    reuses an existing predicate composition — NO new detector.

    Single source of truth shared by KnowledgeBase.upsert_documents (drop new docs) and
    scripts/purge_lex_restricted_kb.py (purge existing PHI chunks). Pass the org-roles staff
    roster as *allowed_names* so a staff possessive ("Harrison Rogers's ...") is not mistaken
    for a care recipient. The W2-01 retrieval backstop remains the second layer for the
    documented residual (a bare name+med/dx with no cue) and for mis-tagged/GM-level content.
    """
    if not is_restricted_lex_ingest(source, sub_entity):
        return False
    from .. import phi_guard  # lazy: keep knowledge_base import-time free of cora.phi_guard
    text = (title or "") + " " + (content or "")
    return phi_guard.non_lex_phi_backstop_trips_individual(text, allowed_names=allowed_names)
