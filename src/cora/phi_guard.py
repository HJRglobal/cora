"""Shared PHI (Protected Health Information) pattern guard for Cora.

Provides a single canonical regex that is the union of all PHI-risk patterns
previously defined in drive_sweep.py and reconciliation_engine.py.  Both
modules import from here so patterns stay in sync.

Patterns cover:
  - Clinical documentation keywords (care plan, clinical note, progress note, etc.)
  - Regulatory / program identifiers (Medicaid, AHCCCS, NPI, ICD-10, etc.)
  - Personal identifiers (SSN, DOB, patient name, client name, etc.)
  - LEX / AZ DDD program-specific terms (DDD client, HCBS client, IEP, ARC, etc.)

Usage:
    from cora.phi_guard import _PHI_PATTERNS, is_phi_risk

    if is_phi_risk(text):
        # skip / quarantine
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Visibility CPA exclusion
# ---------------------------------------------------------------------------
# These individuals are outside counsel / external accounting and must never
# appear as action owners or gap targets in Cora's automated output (Asana
# nudges, reconciliation gaps, knowledge proposals, etc.).
# Use is_visibility_cpa_mention() to check text, or VISIBILITY_CPA_NAMES to
# match against lowercase name strings directly.
# ---------------------------------------------------------------------------

VISIBILITY_CPA_NAMES: frozenset[str] = frozenset({
    "hayden greber",
    "andrew stubbs",
    "sarah bertoglio",
    "emily stubbs",
    "michael dibenedetto",
    "andrew lee",
    "visibility cpa",
    "astubbs",           # email prefix pattern
    "estubbs",           # email prefix pattern
    "hgreber",           # email prefix pattern
})

_VIS_CPA_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in VISIBILITY_CPA_NAMES) + r")\b",
    re.IGNORECASE,
)


def is_visibility_cpa_mention(text: str) -> bool:
    """Return True if *text* mentions any Visibility CPA team member."""
    return bool(_VIS_CPA_PATTERN.search(text))

# Union of all PHI patterns from drive_sweep.py and reconciliation_engine.py,
# plus the canonical additions: patient, medicaid, ahcccs, npi, ssn.
_PHI_PATTERNS = re.compile(
    r"\b("
    # Personal identifiers
    r"ssn|social\s+security|dob|date\s+of\s+birth|patient|client\s+name"
    # Clinical / service documentation
    r"|service\s+note|care\s+plan|clinical\s+note|treatment\s+plan"
    r"|progress\s+note|incident\s+report|assessment|discharge|intake\s+form"
    r"|support\s+plan|prior\s+auth"
    # Diagnosis and medication
    r"|diagnosis|icd-?10|medication"
    # Insurance / program identifiers
    r"|medicaid|ahcccs|member\s?id|provider\s?id|npi"
    # AZ DDD / LEX-specific program terms
    r"|ddd\s+client|hcbs\s+client|iep|arc\b"
    r")\b",
    re.IGNORECASE,
)


def is_phi_risk(text: str) -> bool:
    """Return True if *text* contains any PHI-risk pattern.

    Intended for subject-line and content pre-checks on LEX / Lexington
    inbox emails and Drive files before KB ingestion or reconciliation passes.
    """
    return bool(_PHI_PATTERNS.search(text))


# ---------------------------------------------------------------------------
# LEX-scope billing / authorization / client-status augmentation
# ---------------------------------------------------------------------------
# The base _PHI_PATTERNS above keys on CLINICAL / IDENTIFIER keywords. It
# misses the class of PHI that is administrative on its face but PHI in
# context: a named individual's billing / authorization / eligibility /
# client-status (e.g. "Bob Smith's billing authorization is pending" -- no
# clinical word at all). Tying an authorization / billing / eligibility term
# to a specific person reveals that the person is a Lexington care recipient,
# which is itself PHI.
#
# This is INTENTIONALLY NOT folded into is_phi_risk(): outside LEX scope
# "authorization" / "billing" tied to a name is ordinary business (a retail
# buyer's PO authorization, a vendor's billing). It is opt-in, consumed only
# by the personal-notes save gate (user_notes.resolve_save_scope) inside LEX
# scope or a DM, where erring toward refusal in the most-regulated entity is
# the correct, fail-safe posture.
#
# Doctrine (2026-06-12): a personal name + billing/authorization/eligibility/
# client-status phrasing IS PHI in LEX scope even with zero clinical keywords.
# Added after a live miss: a non-custodian's "Bob Smith's billing
# authorization is pending" was staged for save in #llc-finance instead of
# being refused.
# ---------------------------------------------------------------------------

# Administrative terms that, tied to a specific person, reveal care-recipient
# status (billing/authorization/eligibility/coverage/claims/units/placement).
_LEX_ADMIN_TERM_RE = re.compile(
    r"\b("
    r"billing|billed|invoic\w*"
    r"|authoriz\w*|reauthoriz\w*|prior\s+auth\w*|service\s+auth\w*|auth\b"
    r"|eligib\w*|enroll\w*|reimburs\w*|co-?pay\w*|coverage|deductible"
    r"|claims?|units?\s+of\s+service|service\s+hours|placement|disenroll\w*"
    r")\b",
    re.IGNORECASE,
)

# A specific individual: a possessive proper name ("Bob's" / "Bob Smith's") OR
# an explicit care-recipient noun. ['’] covers straight + curly apostrophe.
_NAME_POSSESSIVE_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}['’]s\b")
_CARE_RECIPIENT_RE = re.compile(
    r"\b(client|patient|member|individual|participant|recipient|guardian|parent)\b",
    re.IGNORECASE,
)

# Client-status phrasing: a care-recipient noun within ~30 chars of a status
# word, in either order ("client status", "member is active", "discharged the
# patient"). Independent signal from the admin-term branch.
_STATUS_WORD = (
    r"status|standing|discharg\w*|admitt\w*|admission|active|inactive|pending|"
    r"approved|denied|terminat\w*|eligib\w*|enrolled"
)
_CLIENT_STATUS_RE = re.compile(
    r"\b(?:client|patient|member|individual|participant|recipient)\b"
    r"[\w\s'’,-]{0,30}\b(?:" + _STATUS_WORD + r")\b"
    r"|\b(?:" + _STATUS_WORD + r")\b"
    r"[\w\s'’,-]{0,30}\b(?:client|patient|member|individual|participant|recipient)\b",
    re.IGNORECASE,
)


def is_lex_billing_status_phi(text: str) -> bool:
    """LEX-scope PHI augmentation (opt-in; NOT part of is_phi_risk).

    True when *text* ties an administrative term (billing / authorization /
    eligibility / coverage / claims / units / placement) to a specific
    individual (a possessive proper name OR a care-recipient noun), OR uses
    explicit client-status phrasing. Catches PHI that carries no clinical
    keyword and so escapes the base patterns. Apply ONLY in LEX scope or a DM.
    """
    if not text:
        return False
    if _LEX_ADMIN_TERM_RE.search(text) and (
        _NAME_POSSESSIVE_RE.search(text) or _CARE_RECIPIENT_RE.search(text)
    ):
        return True
    return bool(_CLIENT_STATUS_RE.search(text))
