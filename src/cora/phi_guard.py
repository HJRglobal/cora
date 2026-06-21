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
# Wellness-overlap terms legitimately appear in F3E Mood / wellness business copy
# ("Mood helps with anxiety"), so the WRITE-gate clinical check (is_clinical_phi)
# EXCLUDES them to avoid over-refusing legit product facts. Their clinical FRAMING
# ("diagnosed with anxiety") is still caught by _DIAGNOSED_WITH_RE, and the scrubber
# (scrub_lex_phi) still redacts them in LEX meeting context where they ARE PHI.
_WELLNESS_OVERLAP_TERMS = frozenset({"anxiety", "depression", "depressive"})
_CLINICAL_DX_RE = re.compile(
    r"\b(?:" + "|".join(
        re.escape(t) for t in _DIAGNOSIS_TERMS if t not in _WELLNESS_OVERLAP_TERMS
    ) + r")\w*",
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


def is_clinical_phi(text: str) -> bool:
    """True if *text* carries clinical PHI that is_phi_risk's keyword set misses.

    Closes the diagnosis/medication gap on the known-answers WRITE gate (WS17-B
    pre-merge fix): is_phi_risk keys on the literal words 'diagnosis'/'medication'
    but NOT bare diagnosis terms (autism / ADHD / nonverbal / Down syndrome),
    'diagnosed with X', or psych-drug NAMES (risperidone, ...). Those detectors
    otherwise live only inside scrub_lex_phi (a redactor), which the write gate
    never calls.

    Entity-agnostic + fail-safe: a missed legit fact is far cheaper than persisting
    clinical PHI into a durable, always-loaded knowledge file. DELIBERATELY narrow to
    avoid over-refusing legitimate F3E / OSN business facts:
      - NO name redaction (would refuse legit possessive names like "Larry's deck").
      - NO dose / med-CONTEXT cue ('dose' / 'mg') -- those appear in F3E/OSN
        supplement copy ("a 200mg dose of caffeine").
      - EXCLUDES the wellness-overlap terms (anxiety / depression) -- F3 Mood's core
        positioning; their clinical FRAMING ("diagnosed with anxiety") is still caught.
    Accepted residuals (covered by the human thumbs-up gate + is_phi_risk /
    is_lex_billing_status_phi): a bare soft-term about a person, and a non-curated
    drug name with no 'medication' keyword.
    """
    if not text:
        return False
    return bool(
        _DOB_RE.search(text)
        or _DIAGNOSED_WITH_RE.search(text)
        or _ICD10_RE.search(text)
        or _CLINICAL_DX_RE.search(text)
        or _MED_NAME_RE.search(text)
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


# ---------------------------------------------------------------------------
# Cue-proximity + care-noun-governed bare-name redaction (B5, 2026-06-17) -- RETRIEVAL-ONLY
# ---------------------------------------------------------------------------
# scrub_lex_phi catches a client name only when IMMEDIATELY preceded by a
# care-recipient noun ("client John") or possessive ("Bob's"). This adds, for a
# NON-custodian's RETRIEVED LEX content ONLY (context_loader._apply_lex_phi_scrub,
# NOT the meeting-capture path), two passes that catch bare client names the
# immediate-noun rule misses.
#
# Hardened after the 2026-06-17 adversarial review, which proved a single
# Title-case-near-cue sweep was wrong in BOTH directions -- it LEAKED admin-cue
# names (units/AHCCCS/EVV), ALLCAPS/accented names, and a client whose given name
# matched a staff first name; and it SHREDDED ordinary prose (sentence-initial
# verbs, the cue words themselves, staff names glued to a trailing word). Two
# passes fix both:
#   PASS 1 (care-noun-governed): a name DIRECTLY after a care-recipient noun is a
#     client -> redact ANY-case form (Title/ALLCAPS/lowercase/accented), unless it
#     is a common word/verb or an exact staff FULL name. Context wins over the
#     roster's first-name guess (so a client "Aaron" after "the client," redacts).
#   PASS 2 (Title-case near a cue): redact a Title-case name within `window` of a
#     cue, guarded so a token that IS a cue word / function word / common ops verb
#     is NOT a name, a staff first name (incl. nicknames) is preserved (NON-governed
#     -> could be staff), and a greedy span keeps a leading staff full-name prefix.
# NOT a non-PHI proper-noun allowlist: a place/vendor near a cue is still redacted
# (fail-safe; Harrison 2026-06-17). NOTE the ALLCAPS/lowercase/accented coverage is
# PASS-1 (governed) ONLY -- PASS 2 is Title-case. Documented residuals (access
# controls -- custodian gate + phi topic-gate + entity-siloing -- are primary, and
# 2.3 already neutralizes the chunk title + deep-link where bare names cluster):
#   (a) a bare client name with NO cue anywhere near it; and
#   (b) a NON-governed ALLCAPS name near a cue (PASS 2 won't match ALLCAPS; broad
#       ALLCAPS matching is deliberately avoided -- it would shred acronyms/entity
#       codes near cues). Both are accepted; not closable by regex without NLP or
#       net-negative over-redaction.

_PHI_CUE_RE = re.compile(
    r"\b(?:client|patient|member|individual|participant|recipient|consumer|guardian"
    r"|parent|caregiver|sessions?|appointments?|appt|iep|isp|behavior\w*|incidents?"
    r"|placements?|discharg\w*|admit\w*|admission|authoriz\w*|auth|eligib\w*|diagnos\w*"
    r"|medications?|meds|prescription\w*|habilitation|hab|respite|goals?"
    # admin / AZ DDD-AHCCCS program cues (review HIGH): care-recipient status leaks
    # via billing / units / program identifiers, not just clinical words.
    r"|units?|billing|billed|invoice\w*|reimburs\w*|claims?|coverage|copay|deductible"
    r"|enroll\w*|disenroll\w*|ahcccs|ddd|evv|olcr|dta|dtt|progress\s+notes?"
    r"|service\s+(?:hours?|code)|plan\s+of\s+care)\b",
    re.IGNORECASE,
)

# Care-recipient noun governing a following name (PASS 1), through light punctuation.
# The NOUN is case-insensitive via (?i:...) but the rest is case-SENSITIVE: a
# multi-word name continuation must start uppercase (Title/ALLCAPS), so a lowercase
# verb run after the noun ("client was present for") is NOT captured as a name.
_CARE_NOUN_RE = re.compile(
    r"\b(?i:client|patient|member|individual|participant|recipient|consumer|guardian"
    r"|parent|caregiver)s?\b[\s,:;.\-]{1,4}"
    r"([A-Za-zÀ-ſ][\wÀ-ſ'’\-]*(?:\s+[A-ZÀ-ſ][\wÀ-ſ'’\-]*){0,2})"
)

# Title-case name (incl. accented start, interior caps "McKenna", apostrophe/
# hyphen), 1-3 words. Bounded {0,1}/{0,2}, no nested unbounded quantifier -> no ReDoS.
_PROPER_NAME_RE = re.compile(
    r"\b[A-ZÀ-ſ][a-zÀ-ſ]+(?:[A-ZÀ-ſ][a-zÀ-ſ]+)?"
    r"(?:\s+[A-ZÀ-ſ][a-zÀ-ſ]+(?:[A-ZÀ-ſ][a-zÀ-ſ]+)?){0,2}"
)

# Common English words that are frequently Title-case in prose -- function words +
# common ops/comms/clinical verbs + a few common nouns. NOT a proper-noun allowlist;
# deliberately omits any word that doubles as a common first name or month
# (will/may/mark/grace/hope/dawn/june/april/august) so those stay redactable.
_NONNAME_STOPWORDS = frozenset({
    # function words / auxiliaries / modals (no name collisions)
    "the", "this", "that", "these", "those", "a", "an", "and", "or", "but", "so",
    "for", "with", "from", "per", "to", "of", "in", "on", "at", "by", "as", "if",
    "then", "than", "is", "are", "was", "were", "be", "been", "being", "has", "have",
    "had", "do", "does", "did", "he", "she", "it", "we", "they", "you", "his", "her",
    "hers", "their", "our", "your", "its", "him", "them", "us", "no", "not", "yes",
    "also", "still", "now", "next", "new", "when", "where", "what", "who", "why",
    "which", "while", "because", "after", "before", "since", "until", "each", "every",
    "all", "any", "some", "more", "most", "please", "thanks", "re", "fwd",
    "should", "would", "could", "can", "cannot", "must", "might", "shall",
    # common ops / comms / clinical verbs (review over-redaction fix)
    "met", "sent", "called", "discussed", "reviewed", "scheduled", "rescheduled",
    "completed", "updated", "submitted", "cancelled", "canceled", "confirmed",
    "ordered", "coordinated", "checked", "added", "planned", "emailed", "approved",
    "logged", "billed", "received", "processed", "attended", "contacted", "spoke",
    "talked", "asked", "noted", "created", "closed", "opened", "started", "finished",
    "arrived", "missed", "requested", "needs", "needed", "visited", "followed",
    "reached", "documented", "entered", "uploaded", "shared", "assigned", "set",
    "got", "made", "took", "gave", "ran", "went", "left", "kept", "held", "booked",
    "filed", "signed", "paid", "owes", "owed", "pending",
    # common nouns frequently capitalized at sentence start or after a cue
    "meeting", "meetings", "notes", "note", "report", "reports", "form", "forms",
    "file", "files", "copy", "team", "plan", "plans", "visit", "visits", "week",
    "weeks", "today", "tomorrow", "update", "status", "summary", "review", "draft",
    "email", "call", "follow", "followup",
})

# Staff first-name nicknames <-> formal forms (review MED: 'Jen' for Jennifer
# Mortensen was redacted). Only activates for names whose counterpart IS on the
# roster, so it cannot shield an arbitrary client name.
_FIRST_NAME_ALIASES = {
    "jennifer": ("jen", "jenny"), "jeffrey": ("jeff",), "michael": ("mike",),
    "robert": ("rob", "bob"), "matthew": ("matt",), "thomas": ("tom",),
    "christopher": ("chris",), "alexander": ("alex",), "daniel": ("dan",),
    "joshua": ("josh",), "nicholas": ("nick",), "jonathan": ("jon",),
    "samantha": ("sam",), "harrison": ("harry",),
}

_CUE_WINDOW = 120  # chars; widened from 40 (review MED) to span multi-clause sentences


def _alias_first_names(first: set[str]) -> set[str]:
    """Nicknames/formal-forms of roster first names (bidirectional; roster-anchored)."""
    extra: set[str] = set()
    for formal, nicks in _FIRST_NAME_ALIASES.items():
        if formal in first:
            extra.update(nicks)
        for n in nicks:
            if n in first:
                extra.add(formal)
                extra.update(x for x in nicks if x != n)
    return extra


def _redact_multi(toks: list[str], full: set[str], first: set[str]) -> str:
    """Multi-token Title-case span: preserve a leading EXACT staff full-name prefix
    (so 'Shaun Hawkins Reviewed' keeps the name), redact the rest token-wise."""
    for n in (3, 2):
        if len(toks) >= n and " ".join(toks[:n]).lower() in full:
            tail = []
            for t in toks[n:]:
                low = t.lower()
                if _PHI_CUE_RE.fullmatch(t) or low in _NONNAME_STOPWORDS or low in first:
                    tail.append(t)
                else:
                    tail.append("[name redacted]")
            return " ".join(toks[:n] + tail)
    return "[name redacted]"


def redact_cue_adjacent_names(
    text: str, allowed_names: set[str] | None = None, window: int = _CUE_WINDOW
) -> str:
    """Redact a bare client name on a NON-custodian's retrieved LEX content (two
    passes -- see module section above). RETRIEVAL-ONLY; do NOT call from the
    meeting-capture path. No-op when the text contains no PHI cue, so ordinary
    operational prose is never touched. Pure transform."""
    if not text:
        return text
    if not _PHI_CUE_RE.search(text):
        return text  # no PHI context anywhere -> ordinary prose untouched
    full, first = _staff_name_index(allowed_names)
    first = first | _alias_first_names(first)

    # PASS 1 -- a name directly governed by a care-recipient noun is a client.
    def _gov(m: "re.Match[str]") -> str:
        name = m.group(1)
        if " " not in name:  # single token
            low = name.lower()
            if low in _NONNAME_STOPWORDS or _PHI_CUE_RE.fullmatch(name):
                return m.group(0)        # "client called", "member session"
        if name.strip().lower() in full:
            return m.group(0)            # an explicit staff full name (rare)
        prefix = m.group(0)[: m.start(1) - m.start(0)]
        return prefix + "[name redacted]"

    out = _CARE_NOUN_RE.sub(_gov, text)

    # PASS 2 -- Title-case name within `window` of a cue (recomputed on PASS-1 out).
    cue_spans = [(mm.start(), mm.end()) for mm in _PHI_CUE_RE.finditer(out)]

    def _near(s: int, e: int) -> bool:
        return any(s <= ce + window and e >= cs - window for cs, ce in cue_spans)

    def _broad(m: "re.Match[str]") -> str:
        span = m.group(0)
        if not _near(m.start(), m.end()):
            return span
        toks = span.split()
        if len(toks) == 1:
            low = toks[0].lower()
            if _PHI_CUE_RE.fullmatch(toks[0]) or low in _NONNAME_STOPWORDS or low in first:
                return span
            return "[name redacted]"
        return _redact_multi(toks, full, first)

    return _PROPER_NAME_RE.sub(_broad, out)
