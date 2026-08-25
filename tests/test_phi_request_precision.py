"""C12 (cq-ee0a88a2185c): the seed guard's precision, and what it must not lose.

Two benign code-queue seeds were guard-refused live on 2026-08-24 -- a Google Ads
invoice-retrieval capability ask and an intake-notes bug report -- and both were
accepted verbatim after a reword that removed nothing of substance. That is two
false refusals in a single session, on a lane whose whole job is to capture build
signal from a human.

THE SHARED PREDICATES ARE NOT TOUCHED. `_PHI_PATTERNS`, `is_phi_risk` and
`is_any_phi` have ~55 consumers between them, roughly half of which are ingestion
or third-party-egress screens where the recall bias is exactly right --
reconciliation_engine even imports the compiled `_PHI_PATTERNS` object directly.
The fix follows the module's own established pattern instead: a narrower sibling
(`is_phi_risk_person_linked` already existed for precisely this reason, "use this
on request-shaped text") extended, plus a request-shaped union, and ONLY the
request-shaped checkpoints repointed at it.

THE BOUNDARY, which these tests exist to hold:
  REQUEST-shaped  = text a person typed at Cora, which gets REFUSED. False
                    refusals are a visible repeated cost and the author can
                    rephrase. -> is_any_phi_request
  AT-REST / EGRESS = what gets written into a KB-ingested artifact, or sent to a
                    third party. Over-refusing costs nothing. -> is_any_phi
"""

from __future__ import annotations

import inspect

import pytest

from cora import code_queue, phi_guard as pg


# The two live refusals, plus the reconstructed wordings that produced them.
BENIGN_REQUESTS = [
    "Google Ads invoice retrieval lane for monthly accounting filing",
    "Google Ads billing: retrieve invoices from the parent manager account each month",
    "Google Ads invoices -- pull them for each individual account at close",
    "Google Ads invoices are not delivered to any recipient we monitor",
    "Google Ads invoice retrieval -- assessment of whether a mail rule helps",
    "Intake-channel team notes write to known-answers under the channel default entity",
    "Intake-channel notes: needs an assessment of whether known-answers loads",
    "Intake channel notes -- the incident report from 8/24 shows the gap",
    "Tommy - can you send me the Q3 revenue assessment by Friday?",
    "Ask Tommy about the invoice for the parent account",
    "What does the AHCCCS policy say about live-in caregivers?",
]

# Everything the narrowing must STILL refuse.
MUST_REFUSE = [
    "Marcus's service hours were authorized through September",
    "the client units of service are approved",
    "can you access the billing authorization for the patient",
    "client status is active",
    "placement for participant is denied",
    "Emily Carter is the member whose claims reimbursement is pending",
    "Bob Smith's billing authorization is pending",
    "member Emily Carter units of service approved",
    "client assessment for the DDD program",
    "AHCCCS incident report for the residential site",
    "the patient was discharged last Tuesday",
    "his ssn is on file",
    "date of birth and diagnosis are in the chart",
    "Marcus AHCCCS is 84213365",
    "His Medicaid, ID 84213365",
    "AHCCCS ID 84213365",
]


@pytest.mark.parametrize("text", BENIGN_REQUESTS)
def test_a_benign_build_request_is_not_refused(text):
    assert pg.is_any_phi_request(text) is False


@pytest.mark.parametrize("text", MUST_REFUSE)
def test_real_phi_is_still_refused(text):
    assert pg.is_any_phi_request(text) is True


# ── the shared predicates are untouched ─────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Q3 revenue assessment",
    "Incident report filed for weekend event",
    "What does the AHCCCS policy say about live-in caregivers?",
])
def test_the_ingestion_screen_still_refuses_the_homonyms(text):
    """These are the terms the REQUEST screen now lets through. On an email
    subject or a Drive filename they are still a fair proxy for a client record
    and over-refusing costs nothing -- so the strict screen must not move."""
    assert pg.is_any_phi(text) is True


def test_reconciliation_still_sees_an_incident_report_as_phi():
    """reconciliation_engine imports the compiled _PHI_PATTERNS object directly,
    so any in-place edit to that pattern would silently change an unrelated
    ingestion path. It was not edited."""
    from cora import reconciliation_engine as re_
    assert bool(re_._PHI_RE.search("Incident report filed for weekend event"))


def test_the_request_union_is_never_looser_than_its_own_members():
    for text in MUST_REFUSE:
        members = (pg.is_phi_risk_person_linked(text), pg.is_clinical_phi(text),
                   pg.is_lex_billing_status_request(text))
        assert pg.is_any_phi_request(text) is any(members)


# ── the programme-id tail, widened in the same change ───────────────────────

@pytest.mark.parametrize("text", [
    "Marcus AHCCCS is 84213365",
    "His Medicaid, ID 84213365",
    "AHCCCS number 900123",
])
def test_a_beneficiary_number_behind_a_programme_name_still_trips(text):
    """COUPLED to the subtraction. The tail regex was anchored with .match() at
    the programme match's end, so one intervening word let a HIPAA beneficiary
    number through. Widening the subtraction without widening this would have
    made the request screen strictly WORSE than the ingestion screen on exactly
    these numbers."""
    assert pg.is_phi_risk_person_linked(text) is True


def test_the_tail_window_is_bounded_not_open_ended():
    """It is adjacency, not "anywhere in the text" -- a programme name in
    sentence one and an unrelated number in sentence five is not a beneficiary
    number."""
    far = "AHCCCS policy overview. " + ("filler words here. " * 6) + "invoice 84213365"
    assert pg.is_phi_risk_person_linked(far) is False


# ── the lane split, asserted structurally ───────────────────────────────────

def test_request_shaped_checkpoints_use_the_request_union():
    src = inspect.getsource(code_queue)
    # the sites that REFUSE a human's typed text
    for needle in (
        'if phi_guard.is_any_phi_request(f"{title} {summary}".strip()):',
        "phi = phi_guard.is_any_phi_request(summary_text)",
        "if phi_guard.is_any_phi_request(question):",
    ):
        assert needle in src, needle


def test_at_rest_screens_stay_strict():
    """These decide what is WRITTEN into the KB-ingested backlog, not whether a
    person is refused. Loosening them would be a different change with a
    different blast radius."""
    src = inspect.getsource(code_queue)
    for needle in (
        "rep_phi = phi_guard.is_any_phi(representative)",
        "if phi_guard.is_any_phi(note):",
    ):
        assert needle in src, needle


def test_a_refused_seed_names_the_predicate_that_fired():
    """A refusal that leaves no trace of WHICH detector fired cannot be tuned --
    both 8/24 false positives had to be diagnosed by reconstructing candidate
    wordings after the fact. Names only; never the text (D-082)."""
    src = inspect.getsource(code_queue)
    assert "phi_guard.which_predicates(" in src
    assert "predicates=%s" in src


def test_which_predicates_reports_the_request_members_too():
    fired = pg.which_predicates("client status is active")
    assert "is_lex_billing_status_request" in fired
    assert pg.which_predicates("") == []


# ── the deliberate, visible diff ────────────────────────────────────────────

def test_the_dm_lane_is_deliberately_left_strict():
    """tests/test_slack_send_dm_staged.py pins three accepted false refusals
    "so the trade-off is visible". The DM path delivers to a THIRD PARTY, so it
    keeps is_any_phi and those pins are untouched by this change -- the diff
    stays confined to the seed lane."""
    assert pg.is_any_phi("Tommy - can you send me the Q3 revenue assessment "
                         "by Friday?") is True
    assert pg.is_any_phi_request("Tommy - can you send me the Q3 revenue "
                                 "assessment by Friday?") is False
