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


# ---------------------------------------------------------------------------
# LEX action-item PHI scrubber (Meeting Action Capture, 2026-06-14)
# ---------------------------------------------------------------------------
# Used by the Fireflies meeting-action-capture pipeline when LEX OPERATIONAL
# meetings are processed (Harrison directive 2026-06-14). Minimum-necessary: a
# captured task should carry the OPERATIONAL action, not transcribe clinical
# detail. This is a best-effort redactor over a SHORT action-item string (a task
# title / one-line note), NOT a transcript. It drops obvious client-identifying
# PHI -- member full names, DOB, diagnoses, medication names -- while keeping
# staff / operational names (passed in `allowed_names`).
#
# It is INTENTIONALLY recall-biased (over-redacts before it under-redacts): in
# the most-regulated entity, dropping a place name's possessive is a far cheaper
# error than leaking a member's diagnosis. It is the text layer of a
# defense-in-depth stack -- NOT a substitute for the LBHS/Part-2 exclusion or
# the project/channel containment rails.

# DOB tied to an explicit birth cue. Standalone dates are NOT touched so an
# operational due date ("by 6/30") survives.
_DOB_RE = re.compile(
    r"\b(?:d\.?o\.?b\.?|date\s+of\s+birth|born(?:\s+on)?)\b[\s:]*"
    r"(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})",
    re.IGNORECASE,
)

# ICD-10 codes require the decimal point -> specific enough to redact anywhere.
_ICD10_RE = re.compile(r"\b[A-TV-Z][0-9]{2}\.[0-9]{1,4}\b")

# Curated diagnosis terms common in AZ DDD / behavioral-health context. "add"
# is deliberately omitted (collides with add/address/additional). \w* absorbs
# plurals / suffixes (autism -> autistic handled by listing the stem).
_DIAGNOSIS_TERMS = [
    "autism", "autistic", "asperger", "asd", "adhd",
    "anxiety", "depression", "depressive", "bipolar", "schizophreni",
    "ptsd", "ocd", "epileps", "seizure disorder", "cerebral palsy",
    "down syndrome", "intellectual disability", "developmental delay",
    "developmental disability", "fetal alcohol", "fragile x",
    "oppositional defiant", "conduct disorder", "psychosis", "psychotic",
    "nonverbal", "non-verbal", "substance use disorder", "substance abuse",
]
_DIAGNOSIS_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _DIAGNOSIS_TERMS) + r")\w*",
    re.IGNORECASE,
)
# "diagnosed with X" / "diagnosis of X" -> keep the cue, redact the diagnosis.
_DIAGNOSED_WITH_RE = re.compile(
    r"\b(diagnos(?:ed|is)\s+(?:with|of)\s+)([A-Za-z][\w\s'-]{0,40}?)(?=[.,;:]|\band\b|$)",
    re.IGNORECASE,
)

# Medication context: keep the cue word, redact the adjacent drug token.
_MED_CONTEXT_RE = re.compile(
    r"\b(medications?|meds|prescriptions?|prescribed|dosage|dose|titrat\w*)\b"
    r"([\s:]+)([A-Za-z][\w-]+)",
    re.IGNORECASE,
)
_DOSE_RE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:mg|mcg|ml|mg/kg)\b", re.IGNORECASE)
# Curated common psych / behavioral meds (recall booster; not exhaustive).
_MED_NAMES = [
    "risperidone", "risperdal", "aripiprazole", "abilify", "adderall",
    "methylphenidate", "ritalin", "concerta", "vyvanse", "strattera",
    "fluoxetine", "prozac", "sertraline", "zoloft", "lexapro", "escitalopram",
    "clonidine", "guanfacine", "intuniv", "lamotrigine", "lamictal",
    "valproate", "depakote", "lithium", "quetiapine", "seroquel",
    "olanzapine", "zyprexa", "clozapine", "haloperidol", "melatonin",
]
_MED_NAME_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(m) for m in _MED_NAMES) + r")\b",
    re.IGNORECASE,
)

# A care-recipient noun immediately followed by a proper name -> drop the name.
_CARE_RECIPIENT_NAME_RE = re.compile(
    r"\b(client|patient|member|individual|participant|recipient|consumer|guardian|parent)"
    r"\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b",
)


def _staff_name_index(allowed_names: set[str] | None) -> tuple[set[str], set[str]]:
    """Return (full-name set, first-name-token set) of staff to PRESERVE."""
    full = {n.strip().lower() for n in (allowed_names or set()) if n and n.strip()}
    first = {n.split()[0] for n in full if n.split()}
    return full, first


def _is_staff_name(name: str, full: set[str], first: set[str]) -> bool:
    """True if *name* should be PRESERVED as a staff/operational name.

    Errs toward NOT-staff (-> redact) for safety: a multi-token name is staff
    only on an exact full-name match; a single token is staff only if it is a
    known staff first name.
    """
    nm = name.strip().lower()
    if nm in full:
        return True
    toks = nm.split()
    return len(toks) == 1 and toks[0] in first


def scrub_lex_phi(text: str, allowed_names: set[str] | None = None) -> str:
    """Best-effort PHI redaction for a SHORT LEX action-item string.

    Redacts DOB, diagnoses (term list + "diagnosed with X" + ICD-10),
    medications (cue+token, dose, curated names), and client-identifying proper
    names (care-recipient-noun + name, and possessive names) that are NOT in
    *allowed_names* (the staff roster). Preserves staff/operational names.

    Pure transform: it may raise on a pathological input -- callers in the
    capture pipeline wrap it in a fail-safe (truncate + "[review for PHI]").
    """
    if not text:
        return text
    full, first = _staff_name_index(allowed_names)
    out = text

    # 1. DOB (explicit birth cue + date)
    out = _DOB_RE.sub("[DOB redacted]", out)
    # 2. "diagnosed with X" / "diagnosis of X" -> keep cue, redact content
    out = _DIAGNOSED_WITH_RE.sub(lambda m: m.group(1) + "[diagnosis redacted]", out)
    # 3. diagnosis terms anywhere
    out = _DIAGNOSIS_RE.sub("[diagnosis redacted]", out)
    # 4. ICD-10 codes
    out = _ICD10_RE.sub("[dx code redacted]", out)
    # 5. medication cue + adjacent token (keep cue, redact the drug). MUST run
    #    before the dose step -- the dose placeholder contains the word "dose",
    #    which is itself a med-context cue and would otherwise re-trigger here.
    out = _MED_CONTEXT_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}[medication redacted]", out
    )
    # 6. curated medication names
    out = _MED_NAME_RE.sub("[medication redacted]", out)
    # 7. dose amounts (last, per the note above)
    out = _DOSE_RE.sub("[dose redacted]", out)

    # 8. care-recipient noun + proper name -> drop the name (keep the noun).
    #    On a STAFF match keep the whole phrase (group 0), incl. the name.
    def _cr(m: "re.Match[str]") -> str:
        return m.group(0) if _is_staff_name(m.group(2), full, first) \
            else f"{m.group(1)} [name redacted]"
    out = _CARE_RECIPIENT_NAME_RE.sub(_cr, out)

    # 9. possessive proper names not on the staff roster -> "[client]'s"
    def _poss(m: "re.Match[str]") -> str:
        name = re.sub(r"['’]s$", "", m.group(0))
        return m.group(0) if _is_staff_name(name, full, first) else "[client]'s"
    out = _NAME_POSSESSIVE_RE.sub(_poss, out)

    return out
