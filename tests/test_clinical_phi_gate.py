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

from cora.phi_guard import is_clinical_phi
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
