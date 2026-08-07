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


# Payer / program NAMES. These are the one group above that names a TOPIC
# rather than anything about a person: every Arizona DDD policy question says
# "AHCCCS" or "Medicaid" the way an F3E question says "Shopify".
#
# They belong in is_phi_risk, which screens email SUBJECTS and Drive FILENAMES
# before KB ingestion -- there, "AHCCCS" in a subject line genuinely correlates
# with client records, and over-refusing costs nothing. They do NOT belong in a
# screen over a REQUEST someone typed, where the same token is the topic. Live
# evidence (cq-a24f9d2210fc, 2026-08-07): three person-free DDD-policy briefs
# were refused at delegated-work intake, and the ONLY match in all three was
# the bare token "AHCCCS".
_PHI_PROGRAM_NAME_RE = re.compile(r"\b(?:medicaid|ahcccs)\b", re.IGNORECASE)

# An identifier riding immediately behind a programme name ("Medicaid ID 12345",
# "AHCCCS #84213365", "Medicaid number 900123"). Anchored with .match() at the
# programme match's end, so it only fires on what directly follows.
_PROGRAM_ID_TAIL_RE = re.compile(
    r"[\s:#-]{0,3}(?:\b(?:id|ids|no|num|number|#)\b[\s:#-]{0,3})?\d{4,}"
    r"|[\s:#-]{0,3}\b(?:id|ids|no|num|number)\b",
    re.IGNORECASE,
)


def is_phi_risk_person_linked(text: str) -> bool:
    """is_phi_risk MINUS the bare payer/program names.

    True when the text carries a PHI signal that says something about a PERSON
    -- an identifier (ssn/dob/member id/npi), clinical documentation, a
    diagnosis or medication, or a care-recipient class ("DDD client"). A
    question ABOUT a program is not one of those.

    Use this on request-shaped text (a delegated-work brief, a typed ask).
    Keep is_phi_risk on ingestion surfaces, where recall beats precision and a
    program name in a filename is a fair proxy for client records.
    """
    if not text:
        return False
    for m in _PHI_PATTERNS.finditer(text):
        if not _PHI_PROGRAM_NAME_RE.fullmatch(m.group(0)):
            return True
        # ...unless the programme name IS the identifier. "Medicaid ID 1234567"
        # and "AHCCCS ID 84213365" are HIPAA beneficiary numbers, and
        # _PHI_PATTERNS only carries the literal "member id" / "provider id", so
        # subtracting the bare programme token removed their ONLY signal. Keep
        # the subtraction for the topic, not for the number.
        if _PROGRAM_ID_TAIL_RE.match(text, m.end()):
            return True
    return False


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

# Every dash a model actually emits: ASCII hyphen plus the Unicode dash block
# (U+2010 hyphen, U+2011 non-breaking, U+2012 figure, U+2013 en, U+2014 em,
# U+2015 horizontal bar). Repeats allowed -- "client--specific" is the same
# compound as "client-specific", and Cora's own prose uses "--" constantly.
_DASHES = "[-­‐-―−﹣－]"
_DASH_CHARS = frozenset("-­‐‑‒–—―−﹣－")
# (?!-\w) excludes a HYPHENATED COMPOUND ADJECTIVE -- "client-specific",
# "member-facing", "patient-level". Those are descriptors; they name no
# individual, and D-050 is about an admin term tied to a SPECIFIC PERSON. Live
# evidence (cq-d30815ee6993, 2026-08-07): a person-free policy brief was refused
# because the model helpfully wrote "No client-specific or PHI content needed"
# -- the brief's own disclaimer that it contains no PHI was the thing that made
# it look like PHI. Excluding compounds is doctrinally correct, not a loosening:
# "client Marcus", "the client's units" and a bare "client" all still match.
_CARE_RECIPIENT_RE = re.compile(
    r"\b(client|patient|member|individual|participant|recipient|guardian|parent)\b"
    # [^\W\d_] is a LETTER, not \w. \w includes digits, and a digit is never a
    # compound adjective -- it is a case NUMBER. "client-047 units of service"
    # and "member-84726 claims reimbursement" are exactly the de-identified-
    # looking references a LEX brief uses for a real person, and \w silenced
    # every belt on them (D-051 HIGH-2). Letters only: "client-specific" is
    # excluded, "client-047" is not.
    rf"(?!{_DASHES}+[^\W\d_])",
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


def is_any_phi(text: str) -> bool:
    """3-predicate PHI union: is_phi_risk OR is_clinical_phi OR is_lex_billing_status_phi.

    Single screening function for any write/egress checkpoint that must not miss PHI
    carried by only ONE of the three narrower detectors. Added 2026-07-30 (Wave-1
    flywheel-conversion calibration, Fork 3 PHI parity-raise): the code-queue's PHI
    screen was single-predicate (is_phi_risk only) and missed named LEX billing/
    authorization/eligibility text with no clinical keyword at all (e.g. "can you
    access Marcus's service hours?" trips is_lex_billing_status_phi but not
    is_phi_risk). gap_detection.py already composes this same union inline at its
    question-side PHI gate; this gives every OTHER checkpoint (code_queue.py, any
    future write/egress gate) the identical, single, fail-closed screen instead of
    each reimplementing the union or -- worse -- using only is_phi_risk.
    """
    if not text:
        return False
    return bool(is_phi_risk(text) or is_clinical_phi(text) or is_lex_billing_status_phi(text))


def which_predicates(text: str) -> list[str]:
    """Names of the union members that trip on *text* -- diagnostics only.

    LEX-61 (2026-08-06): a live false positive could not be tuned because a PHI
    refusal left no trace of WHICH detector fired, and the refused text itself
    must never be persisted (D-082). Logging these names at a refusal site gives
    the next false positive a specific target -- "is_phi_risk fired, the other
    two did not" -- without creating a PHI sink.

    Never use this to DECIDE anything: is_any_phi is the decision function, and
    an empty list here on a fail-closed error path is not an all-clear.
    """
    out: list[str] = []
    if not text:
        return out
    for name, fn in (("is_phi_risk", is_phi_risk),
                     ("is_clinical_phi", is_clinical_phi),
                     ("is_lex_billing_status_phi", is_lex_billing_status_phi)):
        try:
            if fn(text):
                out.append(name)
        except Exception:  # noqa: BLE001 -- a diagnostic never raises
            out.append(f"{name}:error")
    return out


# ---------------------------------------------------------------------------
# Non-LEX PHI backstop — shared by the LIVE retrieval path and the Drive egress
# ---------------------------------------------------------------------------
# Single source of truth for the "a chunk mis-tagged under a NON-LEX entity still
# carries LEX-client PHI" decision. Used by:
#   - context_loader._withhold_non_lex_phi (W2-01, 2026-07-05): the live Slack/DM
#     retrieval backstop for a non-custodian, mirroring the Drive egress below.
#   - drive_materializer._phi_wall non-LEX branch (2026-06-29): the nightly
#     _brain/swept digest backstop on the org-wide-readable Drive store.
#
# _LEX_PROGRAM_CONTEXT_RE was drive_materializer's private _LEX_CONTEXT_RE; it lives
# here now so both egress paths share ONE regex and can never drift.
#
# WHY the billing/status leg needs a program cue (and clinical does not): is_clinical_phi
# is already NARROW (excludes wellness-overlap anxiety/depression so F3 Mood copy passes;
# no dose/name cue). But is_lex_billing_status_phi is by design a LEX-SCOPE-ONLY detector
# — "client" / "member" / "billing" / "invoice" are ordinary commercial words, so firing
# it unconditionally on a BDM/F3E/OSN chunk over-refuses every run. A name+invoice reveals
# care-recipient PHI ONLY when tied to a care program; the cue is that tie.
#
# DELIBERATELY does NOT include is_phi_risk: that base set keys on generic clinical/
# identifier words (assessment / patient / member id / prior auth / medicaid) that appear
# in ordinary non-LEX business content, so applying it to ALL non-LEX chunks would be
# broad over-refusal — the opposite of the NARROW intent. This mirrors _phi_wall's
# non-LEX branch exactly (verify-first, 2026-07-05).
_LEX_PROGRAM_CONTEXT_RE = re.compile(
    r"\b(AHCCCS|DDD|Medicaid|HCBS|Lexington|LBHS|BHRF|behavioral health)\b", re.IGNORECASE
)


def is_lex_program_context(text: str) -> bool:
    """True if *text* carries an explicit Lexington / Medicaid care-PROGRAM cue.

    The gate for the billing/status leg of non_lex_phi_backstop_trips. See the module
    section above for why the billing/status detector needs this tie on a non-LEX chunk.
    """
    return bool(text and _LEX_PROGRAM_CONTEXT_RE.search(text))


def non_lex_phi_backstop_trips(text: str) -> bool:
    """Content-level PHI backstop for a chunk/body carried under a NON-LEX entity tag.

    True when a mis-tagged non-LEX chunk still carries LEX-client PHI:
      - clinical PHI (is_clinical_phi) — ALWAYS, OR
      - named billing / authorization / eligibility / client-status PHI
        (is_lex_billing_status_phi) tied to a Lexington/Medicaid program cue.

    Reuses the existing phi_guard predicates only — NO new detector — and is deliberately
    NARROW so legitimate non-LEX content is not over-refused (F3 Mood wellness copy passes;
    ordinary commercial 'client billing / invoice' vocab without a care-program cue passes).
    Shared by the live retrieval backstop and the Drive egress so the two stay in lockstep.
    """
    if not text:
        return False
    if is_clinical_phi(text):
        return True
    return is_lex_billing_status_phi(text) and is_lex_program_context(text)


# ---------------------------------------------------------------------------
# LIVE-retrieval variant of the non-LEX backstop (D-051 remediation, 2026-07-05)
# ---------------------------------------------------------------------------
# non_lex_phi_backstop_trips (above) is RECALL-biased: is_clinical_phi trips on a BARE
# medication NAME or diagnosis TERM with no identifier. That is correct for the WRITE gate
# (a durable, always-loaded note) and the once-daily Drive/dossier egress (over-drop a whole
# file, retry next run). But context_loader._withhold_non_lex_phi runs on the HIGH-VOLUME
# per-query retrieval path for EVERY non-custodian non-LEX ask, where that recall bias
# SILENTLY WITHHOLDS legitimate OSN/F3E product copy — "sleep gummy contains 3mg melatonin"
# (melatonin is a sold OSN SKU AND a psych-med name), "Focus stack supports ADHD-style
# concentration", "lithium battery pack for the display fridge" — the exact over-refusal the
# slice charter forbids (D-051 findings 3/8). And the billing/status leg fires on a BARE
# aggregate "Lexington member billing volume" with no individual, withholding real holdco
# finance co-scanned from FNDR/HJRG (finding 4).
#
# This variant keeps the SAME PHI catches but tuned for the live path:
#   - HIGH-SPECIFICITY clinical FRAMING trips unconditionally: DOB, ICD-10, "diagnosed with
#     X", medication-CONTEXT ("prescribed X" / "dose"). These are the "clinical framing" the
#     charter says to catch, and are not ordinary product copy.
#   - BARE dx-term / BARE med-NAME trip ONLY with a co-present care-recipient/program cue
#     (a bare product mention is not identifiable PHI).
#   - billing/status trips ONLY with a program cue AND a non-staff INDIVIDUAL (aggregate
#     finance reveals no care recipient).
# is_clinical_phi + is_lex_billing_status_phi + non_lex_phi_backstop_trips are UNCHANGED,
# so the write gate and the Drive/dossier egress keep their stricter (recall-biased) posture.

# Care-recipient noun OR Lexington/Medicaid program cue — the identifier a bare clinical term
# needs before it is treated as PHI on the live path.
_LIVE_CARE_CUE_RE = re.compile(
    r"\b(client|patient|member|individual|participant|recipient|consumer|guardian"
    r"|caregiver|resident"
    r"|AHCCCS|DDD|Medicaid|HCBS|Lexington|LBHS|BHRF|behavioral health)\b",
    re.IGNORECASE,
)


def _reveals_individual_care_recipient(
    text: str, allowed_names: set[str] | None = None
) -> bool:
    """True if *text* ties billing/status to a SPECIFIC individual (not an aggregate).

    A care-recipient noun governing a Title-case name ("client John") OR a possessive
    proper name that is NOT on the staff roster ("Bob Smith's"). Aggregate phrasing
    ("Lexington member billing volume", "client enrollment mix") reveals no individual and
    returns False. Staff possessives (Harrison Rogers's, Justin Moran's — pervasive in the
    holdco finance corpus) are excluded so they do not read as care recipients.
    """
    if not text:
        return False
    if _CARE_RECIPIENT_NAME_RE.search(text):
        return True
    full, first = _staff_name_index(allowed_names)
    for m in _NAME_POSSESSIVE_RE.finditer(text):
        name = re.sub(r"['’]s$", "", m.group(0))
        if not _is_staff_name(name, full, first):
            return True
    return False


def _names_governed_care_recipient(
    text: str, allowed_names: set[str] | None = None
) -> bool:
    """A care-recipient NOUN governing a Title-case name ("client John Smith"), excluding an
    exact staff full-name. Used ONLY by the tag-scoped BILLING leg (below).

    Unlike _reveals_individual_care_recipient, a BARE possessive ("Rita Hill's", "Lowe's",
    "Employee's", "Kuska's") does NOT count: on LBHS/LTS-tagged FINANCIAL business content a
    possessive is overwhelmingly a bookkeeper / vendor / employee, and "billing"/"invoice"/
    "claims" are ordinary business vocab, so possessive + billing + the tag-implied program cue
    over-dropped 19 real business chunks (D-051 re-gate 2026-07-06: bookkeeper AR sheets,
    vendor P&Ls, CFO invoices, Chase wires). A possessive next to a MEDICATION is still PHI —
    that stays on the dx/med leg via _reveals_individual_care_recipient (unchanged).
    """
    if not text:
        return False
    full, first = _staff_name_index(allowed_names)
    for m in _CARE_RECIPIENT_NAME_RE.finditer(text):
        # "recipient <Name>" on a FINANCIAL doc is the banking payee ("recipient Custodian" on a
        # Chase wire), not a care recipient -- exclude it from the billing leg (the clinical
        # dx/med + scrub paths keep the full noun set). D-051 re-gate: this was the last
        # business over-drop (an EBITDA wire-activity PDF).
        if m.group(1).lower() == "recipient":
            continue
        if not _is_staff_name(m.group(2), full, first):
            return True
    return False


def non_lex_phi_backstop_trips_live(
    text: str, allowed_names: set[str] | None = None
) -> bool:
    """Live-retrieval variant of non_lex_phi_backstop_trips (see module section above).

    Same PHI catches, tuned so the high-volume per-query non-LEX path does not over-refuse
    legitimate product copy or aggregate finance. Pass the staff roster as *allowed_names*
    so staff possessives are not mistaken for care recipients.
    """
    if not text:
        return False
    # High-specificity clinical framing — unconditional (not ordinary product copy).
    # NOTE: deliberately does NOT include _DOSE_RE / _MED_CONTEXT_RE — is_clinical_phi
    # itself excludes them, and a supplement dose ("200mg caffeine", "3mg melatonin") is
    # exactly the OSN/F3E product copy this variant must let through.
    if _DOB_RE.search(text) or _ICD10_RE.search(text) or _DIAGNOSED_WITH_RE.search(text):
        return True
    # Bare dx-term / bare med-NAME: PHI when a care/program cue OR a specific INDIVIDUAL is
    # co-present (D-051 re-gate). A bare product mention with NEITHER ("3mg melatonin",
    # "ADHD-style focus", "lithium battery") is not identifiable PHI and passes; a possessive
    # or care-noun-governed client name adjacent to a med/dx ("Jalen's risperidone", "client
    # Marcus is autistic") IS caught.
    #   ACCEPTED RESIDUAL (documented; NOT a regression — the live non-LEX path had NO backstop
    #   before this slice): a BARE full-name-subject or first name next to a med/dx TERM with
    #   no possessive/care-noun/program/DOB/ICD/diagnosed-with ("Marcus Johnson is autistic",
    #   "Kayla started clonidine"). Closing it needs person-name detection that over-refuses
    #   legit OSN/F3E copy where med/dx terms co-occur with named stores/brands ("Sprouts
    #   carries melatonin", "natural Prozac alternative", "ADHD-style Focus stack") — the
    #   co-equal don't-over-refuse mandate. The STRICT predicate (Drive/dossier egress) still
    #   catches all of these; the LEX-channel scrub + custodian gate + entity siloing +
    #   fireflies-first classify_lex_meeting remain the primary net; the BAA/two-Cora split
    #   (Track B) is the durable fix. Flagged for Harrison.
    if (_CLINICAL_DX_RE.search(text) or _MED_NAME_RE.search(text)) and (
        _LIVE_CARE_CUE_RE.search(text)
        or _reveals_individual_care_recipient(text, allowed_names)
    ):
        return True
    # Named billing/status tied to a Lexington/Medicaid program AND a specific individual.
    return (
        is_lex_billing_status_phi(text)
        and is_lex_program_context(text)
        and _reveals_individual_care_recipient(text, allowed_names)
    )


def non_lex_phi_backstop_trips_individual(
    text: str, allowed_names: set[str] | None = None
) -> bool:
    """TAG-SCOPED variant of non_lex_phi_backstop_trips_live for paths that run on content
    ALREADY tagged to a regulated LEX sub-entity (W6-01 Fix-A / D-073: the ingest drop + the
    purge, both scoped to LBHS/LTS-tagged gmail/drive content).

    On tag-scoped content a Lexington/behavioral-health PROGRAM cue is present BY CONSTRUCTION
    (LBHS / BHRF / "behavioral health" are the very keywords detect_sub_entity uses to assign
    the tag — ~66% of tagged chunks carry one), so the LIVE variant's "bare dx/med term + program
    cue" leg degenerates to "bare dx/med term" and OVER-DROPS business docs that merely mention a
    diagnosis/med as a descriptor (a school name "ACHIEVE School for Autism", a job title "Autism
    Behavioral Support Rep", a fee schedule / formulary). So here the bare dx/med leg requires a
    SPECIFIC INDIVIDUAL (a possessive or care-noun-governed non-staff name), NOT the program cue.

    Clinical FRAMING (DOB / ICD-10 / "diagnosed with X") stays UNCONDITIONAL, and named
    billing/status still requires program cue + individual — identical to the LIVE variant. Only
    the bare dx/med leg differs. Business docs with an incidental dx/med term and no named
    individual are KEPT (Harrison's keep-business intent); "client Marcus is autistic" /
    "Jalen's risperidone" / "diagnosed with autism" still trip.

    Do NOT use this on the general (non-tag-scoped) retrieval path — there the program cue is
    RARE and meaningful, and dropping it would weaken the W2-01 backstop.
    """
    if not text:
        return False
    if _DOB_RE.search(text) or _ICD10_RE.search(text) or _DIAGNOSED_WITH_RE.search(text):
        return True
    if (_CLINICAL_DX_RE.search(text) or _MED_NAME_RE.search(text)) and \
            _reveals_individual_care_recipient(text, allowed_names):
        return True
    # Billing/status leg: on tag-scoped financial content the individual MUST be a
    # care-recipient-noun-GOVERNED name ("client John"), NOT a bare possessive (a bookkeeper/
    # vendor/employee possessive + billing vocab is business, not PHI) -- D-051 re-gate.
    return (
        is_lex_billing_status_phi(text)
        and is_lex_program_context(text)
        and _names_governed_care_recipient(text, allowed_names)
    )


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
# Tight window for the LONE-name case in has_care_context_person_name: a single
# capitalised word is only person-evidence when it sits right against a care cue.
_TIGHT_CUE_WINDOW = 24


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


# ---------------------------------------------------------------------------
# Outbound-egress person-name detector (LEX web screen)
# ---------------------------------------------------------------------------
# Proper nouns that are NEVER a person: geography, agencies, programs, and the
# policy vocabulary a legitimate public-policy web query is made of.
#
# SCOPE (load-bearing): this set is consumed ONLY by
# has_care_context_person_name below -- a BLOCK-vs-ALLOW decision on a query
# about to leave the machine. It is deliberately NOT wired into
# redact_cue_adjacent_names / scrub_lex_phi: those decide what a non-custodian
# SEES, and widening their allowlist would weaken the live retrieval scrub.
# Here the failure directions are asymmetric in the other direction -- a false
# positive silently degrades a legitimate DDD-policy web ask to KB-only (the
# LEX-17 dead-end failure mode), while a false negative is still backstopped by
# is_any_phi, which runs FIRST at every call site.
_NONPERSON_PROPER_NOUNS = frozenset({
    # AZ geography + the states/cities LEX operates or asks policy about
    "arizona", "phoenix", "tucson", "mesa", "scottsdale", "chandler", "glendale",
    "tempe", "gilbert", "peoria", "yuma", "flagstaff", "maricopa", "pima",
    "utah", "nevada", "california", "texas", "colorado", "america", "american",
    "united", "states", "state", "county", "city", "north", "south", "east", "west",
    # agencies / programs / payers
    "ahcccs", "ddd", "dds", "medicaid", "medicare", "division", "developmental",
    "disabilities", "department", "economic", "security", "des", "olcr", "evv",
    "hcbs", "iep", "isp", "waiver", "title", "chapter", "section", "article",
    "appendix", "exhibit", "attachment", "policy", "policies", "manual", "manuals",
    "handbook", "guide", "guidance", "bulletin", "memo", "rule", "rules",
    "regulation", "regulations", "statute", "code", "codes", "standard", "standards",
    "requirement", "requirements", "provider", "providers", "agency", "agencies",
    "office", "bureau", "board", "commission", "council", "committee", "administration",
    # service / program vocabulary that is routinely Title-cased in policy prose
    "health", "healthcare", "care", "cost", "containment", "system", "systems",
    "services", "service", "support", "supports", "behavioral", "behavior",
    "habilitation", "respite", "attendant", "companion", "nursing", "therapy",
    "residential", "community", "based", "home", "living", "day", "group",
    "program", "programs", "training", "employment", "transportation", "housing",
    "rate", "rates", "reimbursement", "billing", "unit", "units", "hour", "hours",
    "eligibility", "enrollment", "authorization", "assessment", "plan", "plans",
    "caregiver", "caregivers", "family", "families", "individual", "individuals",
    "quality", "compliance", "licensure", "certification", "revalidation",
    "incident", "incidents", "grievance", "appeal", "appeals", "hearing",
    # Imperative / task verbs (D-051 finding, 2026-08-06). A brief or a web ask
    # OPENS with one of these, capitalized, and _PROPER_NAME_RE cannot tell a
    # sentence-initial verb from a first name -- so "Research what DDD requires
    # for respite" read as a person named "Research" and refused the single
    # most common LEX request shape, including the exact wording the refusal
    # copy tells the user to switch to. Fixed HERE rather than by skipping the
    # sentence-initial token, because "Marcus needs his respite auth renewed"
    # is the counterexample that rule would break.
    "research", "summarize", "summarise", "compile", "investigate", "prepare",
    "analyze", "analyse", "pull", "find", "list", "outline", "identify",
    "write", "build", "check", "confirm", "verify", "gather", "assemble",
    "search", "look", "google", "show", "explain", "describe", "compare",
    "review", "draft", "create", "give", "tell", "get", "read", "collect",
    # List/section openers -- a brief's own scaffolding. "Include deadline..."
    # sat 20 chars from the cue "billing" and read as a person named Include.
    "include", "including", "focus", "cover", "note", "consider", "highlight",
    "flag", "detail", "specify", "address", "explore", "assess", "evaluate",
    # Landmark case names / eponyms that appear in disability-policy questions
    # (same class: a legitimate policy ask silently degrading to KB-only).
    "olmstead", "medicaid's", "rehabilitation",
    # Our OWN entity + brand names. A client is never called "Lexington"
    # (cq-a24f9d2210fc: it false-positived on the entity's own name).
    "lexington", "hjr", "cora", "f3", "osn", "bdm", "ufl",
    # AZ DDD / AHCCCS programme vocabulary that is routinely Title-cased
    "type", "portal", "vendor", "qualified", "enrollment", "apep", "credential",
    "credentials", "attestation", "attestations", "revalidation", "waiver",
    # legal / publication vocabulary (citation-shaped policy questions)
    "administrative", "register", "revised", "statutes", "annotated", "federal",
    "national", "association", "center", "centers", "institute", "university",
    "school", "schools", "education", "social", "human", "children", "child",
    "adult", "adults", "senior", "disability", "medical", "mental", "physical",
    "occupational", "speech", "special", "public", "general", "official",
    # calendar. DELIBERATELY OMITS may/june/april/august/march -- they double as
    # first names, and _NONNAME_STOPWORDS omits them for exactly that reason
    # (see its comment). Listing them here would make "client April" invisible
    # to PASS 1, which is a PHI miss, not a precision win.
    "january", "february", "july", "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
})


# Words that genuinely follow a care-recipient noun WITHOUT naming anyone:
# "member id", "client record", "participant roster". Checked in PASS 1 only,
# where governance by a care noun would otherwise make any following token a
# person. A real given name is never one of these.
_RECORD_NOUNS = frozenset({
    "id", "ids", "number", "numbers", "no", "record", "records", "file", "files",
    "chart", "charts", "name", "names", "roster", "rosters", "list", "lists",
    "count", "counts", "status", "type", "types", "portal", "profile",
    "profiles", "data", "info", "information", "details", "detail",
})


def has_care_context_person_name(text: str, allowed_names: set[str] | None = None,
                                 cue_required: bool = True) -> bool:
    """True when a person-shaped proper name rides in PHI/care context.

    The egress half of redact_cue_adjacent_names: same two signals (a
    care-recipient-noun-governed name, or a Title-case name within the cue
    window) reported as a boolean instead of a redaction, with geography /
    agency / policy proper nouns excluded so a public-policy question
    ("Arizona DDD respite rate policy") is not mistaken for a client mention.

    *cue_required* (D-051 finding F4, 2026-08-06): with a cue required, the
    single most dangerous query shape slips through -- a bare locate-the-person
    lookup with no care vocabulary at all ("Marcus Delgado current address",
    "phone number for Marcus Delgado", "what school Marcus Delgado attends").
    Those are realistic LEX asks AND the one shape where the USER puts the
    client name into the outbound string directly. Pass cue_required=False on
    an egress path to treat ANY non-roster, non-geographic person-shaped name
    as a hit. Over-blocking there degrades silently to KB-only, which is the
    documented-acceptable direction for the LEX web lane.

    Staff names are never a hit -- a LEX teammate asking about their own
    colleague is not client PHI. Pure function; no I/O.
    """
    if not text:
        return False
    if cue_required and not _PHI_CUE_RE.search(text):
        return False
    full, first = _staff_name_index(allowed_names)
    first = first | _alias_first_names(first)

    def _is_person_token(tok: str, governed: bool = False) -> bool:
        """*governed* = the token is directly governed by a care-recipient noun.

        D-051 (2026-08-06), two HIGH findings, one root cause: PASS 1 was
        applying filters that only PASS 2 may apply, making this egress screen
        WEAKER than the display scrub it mirrors. `redact_cue_adjacent_names._gov`
        deliberately skips the staff-first-name set in PASS 1 -- context wins
        over the roster's guess -- and we did not.

          * Staff first names: the roster yields ~28 common given names
            (aaron, alex, dan, eric, jen, matt, sara, shaun, ...). A client
            named Aaron was invisible: "participant Marcus" blocked while
            "participant Aaron" attached, same sentence.
          * Geography/agency nouns: gilbert, chandler, mesa, peoria, glendale
            and tempe are Phoenix-metro cities AND common surnames -- the exact
            market Lexington serves -- as are west/north/day.

        A token GOVERNED by "client/participant/member/consumer" is a client
        regardless of what else the word means, so PASS 1 applies neither set.
        PASS 2 (a bare Title-case name merely near a cue) still applies both --
        there the word genuinely may be a colleague or a place.
        """
        low = tok.lower()
        if low in _NONNAME_STOPWORDS:
            return False
        if not governed and (low in _NONPERSON_PROPER_NOUNS or low in first):
            return False
        return not _PHI_CUE_RE.fullmatch(tok)

    # PASS 1 -- governed by a care-recipient noun ("client Marcus").
    for m in _CARE_NOUN_RE.finditer(text):
        name = (m.group(1) or "").strip()
        if not name or name.lower() in full:
            continue
        # "member id" was reading as a person named "Id" and drawing the
        # person-named refusal for a brief naming nobody. The first fix required
        # the governed name to be CAPITALISED -- which was wrong, and a
        # self-inflicted egress miss: people type lowercase in Slack all day, so
        # "rules for client marcus delgado" then sailed through to the search
        # API. Governance by a care noun is the highest-confidence signal there
        # is and case must not gate it; exclude the RECORD nouns that actually
        # follow those nouns instead.
        if all(t.lower() in _RECORD_NOUNS for t in name.split()):
            continue
        # Same hyphenated-compound case as _CARE_RECIPIENT_RE above, on the
        # other predicate: _CARE_NOUN_RE's separator class includes "-", so
        # "client-specific" parsed as the care noun "client" governing a person
        # named "Specific". Filtered HERE rather than in the shared regex --
        # _CARE_NOUN_RE also drives redact_cue_adjacent_names, and the redactor
        # must not be narrowed by a fix aimed at an intake screen.
        sep = m.group(0)[:m.start(1) - m.start(0)]
        if _DASH_CHARS.intersection(sep) and not any(c.isspace() for c in sep):
            continue
        if any(_is_person_token(t, governed=True) for t in name.split()):
            return True

    # PASS 2 -- Title-case name within the cue window (or anywhere when the
    # caller waived the cue precondition: with no cue in the text there are no
    # spans to be "near", so proximity must not be the filter).
    #
    # EVIDENCE BAR (cq-a24f9d2210fc, 2026-08-07): a LONE Title-case word is not
    # evidence of a person. Measured on three real, entirely person-free DDD
    # policy briefs, this pass produced six false positives -- `Lexington` (the
    # entity's own name), `Provider Type`, `Qualified Vendor`, `Portal`,
    # `Include`, `Focus`. Extending the stopword list each time is an unbounded
    # tail: ordinary prose has an endless supply of capitalised nouns and
    # sentence-initial verbs. A personal name in ungoverned prose is
    # First+Last, so PASS 2 now requires TWO ADJACENT person-shaped tokens, or
    # a possessive.
    #
    # RECALL TRADE, stated plainly: a lone first name with no governing noun
    # and no possessive ("respite units for Madison") is no longer caught HERE.
    # It is still caught when governed by a care-recipient noun (PASS 1, which
    # deliberately keeps single-token detection -- "participant Aaron"), when
    # possessive (below, and is_lex_billing_status_phi), and by the clinical /
    # identifier predicates. That residual is the price of the lane being
    # usable at all; the alternative measured 100% refusal on real briefs.
    cue_spans = [(mm.start(), mm.end()) for mm in _PHI_CUE_RE.finditer(text)]
    # str.rstrip takes a CHARACTER SET, not a suffix -- rstrip("'’s") ate the
    # name's own trailing s ("Marcus's" -> "marcu"), so this branch was dead for
    # every -s name: Marcus, James, Williams, Davis, Harris, Jones, Rogers...
    # i.e. a large share of real surnames AND the worked example in the comment
    # below. "Marcus's home address" reached the search API. Use the suffix
    # substitution this module already uses twice elsewhere.
    possessive = {re.sub(r"['’]s$", "", m.group(0)).strip().lower()
                  for m in _NAME_POSSESSIVE_RE.finditer(text)}
    for m in _PROPER_NAME_RE.finditer(text):
        s, e = m.start(), m.end()
        if cue_required and not any(
                s <= ce + _CUE_WINDOW and e >= cs - _CUE_WINDOW for cs, ce in cue_spans):
            continue
        span = m.group(0)
        if span.strip().lower() in full:
            continue
        toks = span.split()
        # A possessive name is a person at any length ("Marcus's", "Delgado's").
        if span.strip().lower() in possessive and any(_is_person_token(t) for t in toks):
            return True
        # Two ADJACENT unknown person-shaped tokens. A known non-person token
        # does NOT reset the run -- it is skipped -- so "Provider Marcus
        # Delgado" still reads as a name rather than being split by the noun in
        # front of it (D-051 finding: adding stopwords was silently converting
        # 2-token hits into 1-token misses).
        run = 0
        for t in toks:
            if not _is_person_token(t):
                continue
            run += 1
            if run >= 2:
                return True
        # A LONE person-shaped token is still a hit when it sits TIGHT against a
        # care cue ("respite units for Madison"). The two-token bar exists
        # because a lone capitalised word ANYWHERE in long policy prose is
        # noise; within a few words of a care cue it is not. Narrower than the
        # 120-char window PASS 2 uses for proximity, deliberately -- measured
        # against the three real policy briefs, which stay clean.
        if run == 1:
            for cs, ce in cue_spans:
                if s <= ce + _TIGHT_CUE_WINDOW and e >= cs - _TIGHT_CUE_WINDOW:
                    return True
    return False
