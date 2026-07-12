"""Outbound channel-scope CONTENT guard for Cora's Q&A answer path (F-08 family).

Cora enforces confidentiality at the TOOL layer (the QBO tier gate, the
`f3e_creator_crm` entity gate, the 4 dashboard tools' `dashboard_access` gate) and
via deterministic pre-LLM keyword guards (`cross_entity_guard`, `sibling_guard`).
The mega-smoke (2026-07-11) proved a systemic gap: the KB-retrieval + personal-
context ANSWER path had NO channel-scope content guard. Because the asker is often
Harrison (authorized for everything at the PERSON level), the model would surface
confidential content INTO WHATEVER CHANNEL HE ASKED IN, regardless of the channel's
other members. Authorization is evaluated per-person; exposure happens per-channel.

Confirmed leaks, one root cause each:
  * F-08  personal insurance figures (OneAmerica) into #cora-build
  * F-08  capital-program terms ($25M / $500K seat / ambassador %) into #cora-build
  * F-12  company revenue ($320,615) into TIER_3 #f3-athletes
  * F-13  F3E creator CRM into #osn-leadership (after a correct tool self-refusal)
  * F-10  travel-points figures into #cora-build
  * F-11  confidential-dashboard existence/purpose enumerated in-channel

This module is the OUTBOUND twin of the retrieval-side `context_loader`
`_apply_lex_phi_scrub` / `_withhold_non_lex_phi` PHI backstop: it evaluates the
COMPOSED ANSWER against the CHANNEL (tier / entity / channel_name / is_dm) and
REFUSES a defined set of confidential content classes when the channel does not
permit them. It fires on the ANSWER regardless of whether a tool self-refused
(F-13) and regardless of the asker's person-level authorization (keyed on CHANNEL,
not asker -- that is the whole point).

Design (same doctrine as cross_entity_guard / dashboard_access -- code-level, not
prompt-only, D-034):
  * Channel permission for the dashboard-backed classes (personal insurance,
    capital program, travel points, cross-entity CRM/content) is delegated to the
    SINGLE source of truth `dashboard_access.check_dashboard_access` -- so this
    guard and the tool-layer gate can never drift. A class is "not permitted here"
    exactly when its dashboard would refuse here.
  * Company financials (no dashboard) key on the channel TIER: permitted in a
    TIER_1 channel (leadership / finance / founder / build) OR a founder/aggregator
    (FNDR/HJRG) channel OR a DM -- the post-LLM backstop for the pre-LLM
    `user_access` financials deflection and the QBO tool tier gate, which a
    KB-sourced figure can slip past. Refused only in a non-founder entity channel
    below TIER_1 (e.g. #f3-athletes -- the F-12 surface).
  * On the FIRST class that trips, the ENTIRE answer is replaced with a leak-free,
    class-appropriate refusal (surgical figure-redaction is fragile -- a "93% LTV"
    survives even when the dollar amount is stripped). First-trip wins.
  * Detectors are HIGH-PRECISION and lean fail-safe (refuse) ONLY on a specific
    confidential signature, because over-refusal of legitimate business answers is
    the co-equal risk (the phi_guard lesson). See each detector's notes.

DMs: a DM is 1:1, so there are no "other members" to leak to. The dashboard-backed
classes still run in DMs (dashboard_access allows Harrison's DM for the personal
dashboards and refuses everyone else -- correct in a DM too). Company financials
(class `company_financials`) is NOT applied in a DM: the leak concern is channel
members, and the DM-financials policy is already owned by the pre-LLM W2-02
deflection. The Tier-2 owner-mail GRANT path is exempt entirely (1:1, owner-scoped,
already access-controlled + PHI-dropped) -- the caller passes skip=True there.
"""

from __future__ import annotations

import logging
import re

from . import dashboard_access

log = logging.getLogger(__name__)

# Founder / portfolio-aggregator entities. Their channels (e.g. #founder-operations,
# which the function-namer misclassifies as TIER_3) are legitimate portfolio-oversight
# surfaces where company financials belong -- so company_financials is permitted there
# regardless of the computed tier (avoids regressing S2.10, the founder-ops portfolio
# read that correctly PASSED). Entity channels (F3E/OSN/...) get the strict tier rule.
_FOUNDER_ENTITIES: frozenset[str] = frozenset({"FNDR", "HJRG"})


# ── Money-figure primitive ────────────────────────────────────────────────────
# A "substantial" money figure: a thousands-separated amount ($1,000+), a 4+ digit
# integer amount, or a K/M/B-suffixed amount. Deliberately does NOT match a small
# retail price ("$36.99") so a product-price answer (S2.1 PASS) never trips.
_MONEY_FIGURE_RE = re.compile(
    r"\$\s?(?:"
    r"\d{1,3}(?:,\d{3})+(?:\.\d+)?"      # $1,000 / $320,615.00
    r"|\d{4,}(?:\.\d+)?"                 # $25000
    r"|\d+(?:\.\d+)?\s?[KkMmBb]\b"       # $25M / $500K / $1.2B
    r")"
)


def _has_money_figure(text: str) -> bool:
    return bool(_MONEY_FIGURE_RE.search(text))


# ── Class: personal insurance (OneAmerica whole-life) ─────────────────────────
# Dashboard: oneamerica-whole-life-portfolio (PERSONAL, DM-to-Harrison only).
# Naming the insurer / whole-life / a policy term IS itself the leak (also covers
# the F-11 "enumerated the dashboard by name" case). "cash value" is the one term
# that can appear commercially, so it requires a co-present money figure.
_ONEAMERICA_STRONG_RE = re.compile(
    r"\bOneAmerica\b|\bwhole[\s-]?life\b|\bsurrender\s+value\b"
    r"|\bdeath\s+benefit\b|\bpolicy\s+loans?\b|\bwhole-?life\s+portfolio\b",
    re.IGNORECASE,
)
_CASH_VALUE_RE = re.compile(r"\bcash\s+value\b", re.IGNORECASE)


def _trips_personal_insurance(text: str) -> bool:
    if _ONEAMERICA_STRONG_RE.search(text):
        return True
    return bool(_CASH_VALUE_RE.search(text) and _has_money_figure(text))


# ── Class: capital program (F3 ambassador-seat raise) ─────────────────────────
# Dashboard: f3-capital-program (HIGHLY_CONFIDENTIAL, DM-to-Harrison only).
# The signature is the ambassador-SEAT INVESTMENT structure ($ seat + equity/%/
# valuation), or the literal program / cap-table concept. Deliberately NOT bare
# "valuation" (would over-refuse a legit HJRPROD deal answer -- S2.8 correctly
# withheld valuation) and NOT bare "ambassador" (that is the CREATOR program, a
# different confidential class -- see cross-entity CRM below).
_CAPITAL_PROGRAM_RE = re.compile(
    r"\bcapital\s+program\b|\bcap\s+table\b|\bcapitalization\s+table\b"
    r"|\bprice\s+per\s+share\b|\bper[\s-]share\b",
    re.IGNORECASE,
)
_CAPITAL_SEAT_RE = re.compile(r"\bseats?\b", re.IGNORECASE)
_CAPITAL_EQUITY_RE = re.compile(
    r"\bequity\b|\bstakes?\b|\bvaluation\b|%|\bownership\b", re.IGNORECASE
)


def _trips_capital_program(text: str) -> bool:
    if _CAPITAL_PROGRAM_RE.search(text):
        return True
    # $ investment SEAT priced with an equity/% signal = the ambassador-seat raise.
    return bool(
        _CAPITAL_SEAT_RE.search(text)
        and _has_money_figure(text)
        and _CAPITAL_EQUITY_RE.search(text)
    )


# ── Class: travel points ──────────────────────────────────────────────────────
# Dashboard: travel-points-optimizer (PERSONAL, DM-to-Harrison only).
# Highly specific loyalty-program terms -- negligible false-positive risk.
_TRAVEL_POINTS_RE = re.compile(
    r"\bCompanion\s+Pass\b|\bA-?List\s+Preferred\b|\bA-?List\b"
    r"|\brapid\s+rewards\b|\bairline\s+miles\b",
    re.IGNORECASE,
)


def _trips_travel_points(text: str) -> bool:
    return bool(_TRAVEL_POINTS_RE.search(text))


# ── Class: F3E creator / sponsorship CRM ──────────────────────────────────────
# Dashboard: f3-creator-sponsorship-command-center (ENTITY F3E; allowed in
# f3-athletes / f3e-leadership / founder-operations + FNDR/HJRG). The F-13 leak
# dumped the F3E creator roster into an OSN channel. Signature = the creator/
# sponsorship CRM concept or its Airtable base id. Split from content-pipeline
# (below) because the two have DIFFERENT channel scopes -- checking one dashboard
# for both would under-refuse content-pipeline data in an athlete channel.
_CREATOR_CRM_RE = re.compile(
    r"\bcreator\s+CRM\b|\bcreator\s+roster\b|\bsponsorship\s+(?:pipeline|command\s+center)\b"
    r"|\bambassador\s+(?:roster|program|command\s+center)\b",
    re.IGNORECASE,
)
_CREATOR_BASE_ID_RE = re.compile(r"\bappwF6W6eVTvPFjct\b")


def _trips_creator_crm(text: str) -> bool:
    return bool(_CREATOR_CRM_RE.search(text) or _CREATOR_BASE_ID_RE.search(text))


# ── Class: founder content pipeline ───────────────────────────────────────────
# Dashboard: f3-content-pipeline (FOUNDER_OPS; founder-operations + FNDR/HJRG
# only -- NOT the F3E channels the creator CRM allows). Signature = the content-
# pipeline concept or its Airtable base id.
_CONTENT_PIPELINE_RE = re.compile(
    r"\bcontent\s+pipeline\b|\bcontent\s+calendar\b|\bfreelancer\s+deliverables?\b",
    re.IGNORECASE,
)
_CONTENT_BASE_ID_RE = re.compile(r"\bappxbEBjIBf8Wwlbd\b")


def _trips_content_pipeline(text: str) -> bool:
    return bool(_CONTENT_PIPELINE_RE.search(text) or _CONTENT_BASE_ID_RE.search(text))


# ── Class: company financial figures (no dashboard; tier-gated) ───────────────
# A company-level P&L / revenue / cash figure. Requires a company-finance TERM AND
# a substantial money figure so a product price ("$36.99", S2.1) or a term with no
# figure ("we want to grow revenue") never trips. Applied only in NON-DM,
# non-TIER_1 channels (the F-12 multi-member exposure surface).
_COMPANY_FINANCE_TERM_RE = re.compile(
    r"\b(?:gross\s+revenue|net\s+revenue|total\s+revenue|revenue|gross\s+sales"
    r"|net\s+income|gross\s+profit|net\s+profit|operating\s+income|EBITDA"
    r"|profit\s+and\s+loss|P&L|gross\s+margin|payroll|net\s+worth"
    r"|MRR|ARR|ending\s+cash|cash\s+(?:position|balance|on\s+hand))\b",
    re.IGNORECASE,
)


def _trips_company_financials(text: str) -> bool:
    return bool(_COMPANY_FINANCE_TERM_RE.search(text) and _has_money_figure(text))


# ── Refusals (leak-free; never name the store / platform / dashboard) ─────────
_REFUSE_PERSONAL = (
    "I keep your personal financial details to our DMs -- ask me there and I'll pull it up."
)
_REFUSE_CAPITAL = (
    "I can't get into the capital-raise details in this channel -- ask me in a DM."
)
_REFUSE_TRAVEL = (
    "I keep that personal to our DMs -- ask me there."
)
_REFUSE_CRM = (
    "That's not something I can pull into this channel."
)
_REFUSE_CONTENT = (
    "That's not something I can pull into this channel."
)
_REFUSE_FINANCIALS = (
    "I can't share company financial figures in this channel -- those live in a "
    "finance or leadership channel, or ask me in a DM."
)


# Ordered so the most-confidential class wins a tie (personal/capital before the
# broader company-financials backstop). Each entry: (class_name, detector,
# dashboard_id_or_None, refusal). dashboard_id None => tier-gated (company fin).
_CLASSES: tuple[tuple[str, object, str | None, str], ...] = (
    ("personal_insurance", _trips_personal_insurance,
     "oneamerica-whole-life-portfolio", _REFUSE_PERSONAL),
    ("capital_program", _trips_capital_program,
     "f3-capital-program", _REFUSE_CAPITAL),
    ("travel_points", _trips_travel_points,
     "travel-points-optimizer", _REFUSE_TRAVEL),
    ("creator_crm", _trips_creator_crm,
     "f3-creator-sponsorship-command-center", _REFUSE_CRM),
    ("content_pipeline", _trips_content_pipeline,
     "f3-content-pipeline", _REFUSE_CONTENT),
    ("company_financials", _trips_company_financials, None, _REFUSE_FINANCIALS),
)


def guard_outbound(
    text: str,
    *,
    entity: str,
    tier: str,
    channel_name: str,
    user_id: str,
    is_dm: bool,
) -> tuple[str, str | None]:
    """Evaluate a composed answer against the channel; return (text, tripped_class).

    If a confidential content class is present AND the channel does not permit it,
    return (refusal_string, class_name). Otherwise return (text, None) unchanged.

    - Dashboard-backed classes: permitted exactly when
      dashboard_access.check_dashboard_access(dash_id, user_id, channel_name)
      returns None (allowed). Single source of truth for channel scoping.
    - company_financials: permitted in a DM (no other members; W2-02 owns the
      DM-financials policy pre-LLM) or in a TIER_1 channel; refused otherwise.

    Pure + fail-safe: on any internal error the text is returned unchanged (a guard
    crash must never drop a legitimate answer), but each detector is a plain regex
    so errors are not expected.
    """
    if not text or not isinstance(text, str):
        return text, None
    for class_name, detector, dash_id, refusal in _CLASSES:
        try:
            if not detector(text):  # type: ignore[operator]
                continue
        except Exception:  # noqa: BLE001 -- a detector must never drop an answer
            log.exception("channel_content_guard: detector %s crashed", class_name)
            continue
        if dash_id is not None:
            # Delegate channel scoping to the single dashboard-access source of truth.
            if dashboard_access.check_dashboard_access(dash_id, user_id, channel_name) is None:
                continue  # this channel is permitted for this class
        else:
            # company_financials: allowed in a DM (no other members; the pre-LLM
            # W2-02 deflection owns the DM-financials policy), a TIER_1 channel, or
            # a founder/aggregator (FNDR/HJRG) channel like #founder-operations.
            if is_dm or tier == "TIER_1" or entity in _FOUNDER_ENTITIES:
                continue
        log.warning(
            "channel_content_guard: REFUSED class=%s channel=#%s entity=%s tier=%s user=%s",
            class_name, channel_name, entity, tier, user_id,
        )
        return refusal, class_name
    return text, None
