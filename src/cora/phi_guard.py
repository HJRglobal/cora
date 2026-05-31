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
