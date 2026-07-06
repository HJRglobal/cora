"""WS17-B pre-merge fix: clinical-PHI write gate (is_clinical_phi + write paths).

Independent pre-merge verification found the known-answers WRITE gate caught the
billing/auth/client-name PHI class but NOT the clinical diagnosis/medication class
(autism / ADHD / nonverbal / Down syndrome / risperidone) -- those detectors lived
only in scrub_lex_phi, which the write gate never calls. These tests assert the new
is_clinical_phi predicate closes that hole at the write gates WITHOUT over-refusing
legitimate F3E/OSN business facts (F3 Mood "anxiety" positioning, supplement doses,
possessive business names).
"""

from __future__ import annotations

import pytest

from cora.phi_guard import (
    is_clinical_phi,
    is_lex_program_context,
    non_lex_phi_backstop_trips,
    non_lex_phi_backstop_trips_live,
)
from cora import gap_autofill as ga


# -- is_clinical_phi: the diagnosis/medication class IS caught --------------------

@pytest.mark.parametrize("text", [
    "Bob has autism",
    "The new client Marcus was diagnosed with ADHD",
    "Sarah is nonverbal so we adjusted her plan",
    "Client Jake has Down syndrome",
    "We put Tyler on risperidone last month",
    "started on adderall and clonidine",
    "history of seizure disorder",
    "DOB: 03/14/2015 for the new intake",
    "coded ICD-10 F84.0 in the chart",
])
def test_clinical_phi_is_caught(text):
    assert is_clinical_phi(text) is True


# -- is_clinical_phi: legit F3E/OSN/business facts are NOT over-refused -----------

@pytest.mark.parametrize("text", [
    "F3 Mood helps reduce anxiety and stress",            # F3 Mood core positioning
    "Mood targets stress and depression after a long day",  # wellness overlap excluded
    "Recommended dose of caffeine is 200mg per can",      # supplement dose, not a drug
    "Each can has a 200mg dose of natural caffeine",
    "Larry's content calendar is due Friday",             # possessive business name
    "Jason's retail plan covers GNC and Five Below",
    "The buyer PO is authorized for next week",
    "$75 free shipping threshold across all three domains",
    "OSN Gilbert & Warner store revenue is up 12% MoM",
    "",
])
def test_legit_business_facts_pass(text):
    assert is_clinical_phi(text) is False


# -- apply_contributed_note: clinical PHI is REFUSED at the durable write ---------

@pytest.mark.parametrize("text", [
    "Bob has autism",
    "Marcus was diagnosed with ADHD",
    "Sarah is nonverbal",
    "Jake has Down syndrome",
    "We put Tyler on risperidone",
])
def test_contributed_note_refuses_clinical_phi(tmp_path, monkeypatch, text):
    # Entity-agnostic: even a non-LEX (OSN) contribution carrying clinical PHI must
    # NOT persist to a durable, always-loaded known-answers file.
    monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path))
    ok, summary = ga.apply_contributed_note({"entity": "OSN", "text": text})
    assert ok is False and "PHI" in summary
    assert not (tmp_path / "osn.md").exists()


# -- apply_contributed_note: clinical-adjacent business facts still persist -------

@pytest.mark.parametrize("text", [
    "F3 Mood helps reduce anxiety and stress",
    "Each can has a 200mg dose of natural caffeine",
    "Larry's content calendar is due Friday",
])
def test_contributed_note_keeps_clinical_adjacent_business_facts(tmp_path, monkeypatch, text):
    monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path))
    ok, _ = ga.apply_contributed_note({"entity": "F3E", "text": text})
    assert ok is True
    assert (tmp_path / "f3e.md").exists()


# -- is_lex_program_context: the Lexington/Medicaid care-program cue (W2-01) ------

@pytest.mark.parametrize("text", [
    "AHCCCS approved the claim",
    "DDD service authorization",
    "Medicaid reimbursement",
    "HCBS placement pending",
    "the Lexington intake queue",
    "LBHS billing backlog",
    "BHRF residential program",
    "behavioral health services provider",
])
def test_lex_program_context_true(text):
    assert is_lex_program_context(text) is True


@pytest.mark.parametrize("text", [
    "the wholesale energy-drink order",
    "our monthly billing cycle",
    "GNC and Five Below retail plan",
    "",
])
def test_lex_program_context_false(text):
    assert is_lex_program_context(text) is False


# -- non_lex_phi_backstop_trips: the W2-01 / W6-06 store-defense predicate --------
# TRIP direction: a mis-tagged non-LEX chunk still carrying LEX-client PHI.

@pytest.mark.parametrize("text", [
    # clinical PHI trips ALWAYS (no program cue needed)
    "The individual takes lamotrigine for a seizure disorder",
    "New intake was diagnosed with autism",
    "coded ICD-10 F84.0 in the chart",
    # named billing/status PHI + a Lexington/Medicaid program cue trips
    "The client's DDD service authorization is still pending",
    "Testcase Charlie's AHCCCS eligibility was denied",
])
def test_non_lex_backstop_trips(text):
    assert non_lex_phi_backstop_trips(text) is True


# PASS direction: legit non-LEX content is NOT over-refused (the F3 Mood trap +
# ordinary commercial billing without a care-program cue + aggregate program finance).

@pytest.mark.parametrize("text", [
    "F3 Mood helps take the edge off everyday anxiety and supports calm focus",
    "Mood targets stress and depression after a long day",
    "The client's invoice for the wholesale energy-drink order is past due",
    "Our standard monthly billing cycle closes on the 30th",
    "Medicaid reimbursement rates increased across the board this quarter",
    "Each can has a 200mg dose of natural caffeine",
    "",
])
def test_non_lex_backstop_passes(text):
    assert non_lex_phi_backstop_trips(text) is False


def test_non_lex_backstop_billing_needs_program_cue():
    """The billing/status leg fires ONLY with a program cue — the sole discriminator
    between care-recipient PHI and ordinary commercial 'client billing' vocab."""
    commercial = "The client's invoice for the retail order is overdue"
    care = "The client's invoice for DDD services is overdue"
    assert non_lex_phi_backstop_trips(commercial) is False
    assert non_lex_phi_backstop_trips(care) is True


# -- non_lex_phi_backstop_trips_LIVE: the per-query variant (D-051 findings 3/4/8) --------
# Must NOT over-refuse OSN/F3E product copy or aggregate finance; still catches clinical
# framing, cue-present clinical terms, and named-individual program billing.

@pytest.mark.parametrize("text", [
    "Our new sleep gummy contains 3mg melatonin per serving",   # melatonin = OSN SKU
    "The Focus stack supports ADHD-style concentration",        # bare dx-term, no cue
    "F3 Mood is a calm blend for PTSD-adjacent stress",         # bare dx-term, no cue
    "The lithium battery pack for the display fridge failed QA",  # med-name, no cue
    "Recommended dose of caffeine is 200mg per can",            # dose, not clinical
    "Lexington member billing volume is up and the HJRG fee tracks with it",  # aggregate finance
    "The client's invoice for the wholesale order is past due", # commercial, no program cue
    "",
])
def test_live_backstop_passes_product_and_aggregate(text):
    assert non_lex_phi_backstop_trips_live(text) is False


@pytest.mark.parametrize("text", [
    "New intake was diagnosed with ADHD last week",             # clinical FRAMING, unconditional
    "coded ICD-10 F84.0 in the chart",
    "DOB 03/15/1990 on the intake form",
    "The DDD participant is on melatonin and clonidine at bedtime",  # med + program cue
    "client John takes risperidone 2mg",                        # med + care-noun cue
    "Client John Smith's DDD service authorization is still pending",  # named individual + program billing
])
def test_live_backstop_catches_real_phi(text):
    assert non_lex_phi_backstop_trips_live(text) is True


def test_live_backstop_excludes_staff_possessive():
    """A STAFF possessive tied to Lexington billing (pervasive in holdco finance) is NOT
    treated as a care recipient when the roster is supplied (finding 4)."""
    text = "Harrison Rogers's Lexington billing summary shows the management fee is current"
    # No roster -> the possessive reads as an individual -> trips (over-refuse).
    assert non_lex_phi_backstop_trips_live(text) is True
    # With the staff roster -> Harrison Rogers is excluded -> aggregate finance passes.
    assert non_lex_phi_backstop_trips_live(text, allowed_names={"Harrison Rogers"}) is False


@pytest.mark.parametrize("text", [
    "Jalen's risperidone dose was increased last week",   # possessive name + med
    "The client, Marcus, is on clonidine",                # care-noun cue + med
    "client Jalen is autistic",                           # care-noun-governed name + dx
])
def test_live_backstop_catches_named_med_without_program_cue(text):
    """D-051 re-gate: a med/dx term next to a possessive or care-noun-governed name is PHI
    even with no Lexington/Medicaid program cue."""
    assert non_lex_phi_backstop_trips_live(text) is True


@pytest.mark.parametrize("text", [
    "Our sleep gummy contains 3mg melatonin",             # OTC SKU, no name/cue -> pass
    "The Focus stack supports ADHD-style concentration",  # dx-term product framing -> pass
    "lithium battery pack for the display fridge",        # med-name word, supply chain -> pass
])
def test_live_backstop_named_med_fix_does_not_over_refuse(text):
    """The re-gate fix must not newly over-refuse product copy (no name/cue -> pass)."""
    assert non_lex_phi_backstop_trips_live(text) is False
