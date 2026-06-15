"""Tests for phi_guard.scrub_lex_phi -- LEX action-item PHI scrubber (2026-06-14)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.phi_guard import scrub_lex_phi

STAFF = {"Shaun Hawkins", "Harrison Rogers", "Aaron Ferrucci", "Jennifer Mortensen"}


class TestScrubLexPhi:
    def test_empty_and_none(self):
        assert scrub_lex_phi("") == ""
        assert scrub_lex_phi(None) is None

    def test_diagnosis_term_redacted(self):
        out = scrub_lex_phi("Update the autism support plan", STAFF)
        assert "autism" not in out.lower()
        assert "[diagnosis redacted]" in out
        assert "support plan" in out

    def test_diagnosed_with_phrase_redacted(self):
        out = scrub_lex_phi("Member was diagnosed with severe bipolar disorder", STAFF)
        assert "bipolar" not in out.lower()
        assert "diagnos" in out.lower()  # cue kept

    def test_dob_redacted(self):
        out = scrub_lex_phi("Confirm DOB 03/14/2009 for enrollment", STAFF)
        assert "03/14/2009" not in out
        assert "[DOB redacted]" in out

    def test_due_date_survives(self):
        """A non-DOB date (operational due date) must NOT be redacted."""
        out = scrub_lex_phi("Send the form by 6/30", STAFF)
        assert "6/30" in out

    def test_icd10_code_redacted(self):
        out = scrub_lex_phi("Bill under F84.0 this month", STAFF)
        assert "F84.0" not in out
        assert "[dx code redacted]" in out

    def test_medication_context_redacted(self):
        out = scrub_lex_phi("Refill medication risperidone", STAFF)
        assert "risperidone" not in out.lower()
        assert "[medication redacted]" in out

    def test_med_name_redacted_anywhere(self):
        out = scrub_lex_phi("Pick up Adderall from pharmacy", STAFF)
        assert "adderall" not in out.lower()

    def test_dose_redacted(self):
        out = scrub_lex_phi("Increase to 10mg daily", STAFF)
        assert "10mg" not in out.lower()
        assert "[dose redacted]" in out

    def test_care_recipient_name_dropped(self):
        out = scrub_lex_phi("Schedule a visit for client John Doe", STAFF)
        assert "John Doe" not in out
        assert "client [name redacted]" in out

    def test_possessive_client_name_redacted(self):
        out = scrub_lex_phi("Submit Bob Smith's authorization", STAFF)
        assert "Bob Smith" not in out
        assert "[client]'s" in out

    def test_staff_possessive_preserved(self):
        out = scrub_lex_phi("Shaun's checklist needs review", STAFF)
        assert "Shaun's" in out

    def test_staff_first_name_in_care_context_preserved(self):
        """A staff first name should not be redacted even after a care noun."""
        # "guardian Aaron" -> Aaron is staff -> preserved.
        out = scrub_lex_phi("Coordinate with guardian Aaron", STAFF)
        assert "Aaron" in out

    def test_staff_full_name_after_care_noun_preserved(self):
        out = scrub_lex_phi("parent Harrison Rogers will attend", STAFF)
        assert "Harrison Rogers" in out

    def test_combined_member_name_and_diagnosis(self):
        out = scrub_lex_phi(
            "Update Bob Smith's autism plan; refill medication abilify; DOB 01/02/2010",
            STAFF,
        )
        assert "Bob Smith" not in out
        assert "autism" not in out.lower()
        assert "abilify" not in out.lower()
        assert "01/02/2010" not in out

    def test_no_allowed_names_redacts_all_possessives(self):
        out = scrub_lex_phi("Shaun's task", set())
        assert "[client]'s" in out  # no staff list -> treated as non-staff

    def test_operational_text_unchanged(self):
        text = "Order new reflective triangles for the vans"
        assert scrub_lex_phi(text, STAFF) == text

    def test_add_not_overmatched(self):
        """'add'/'address'/'additional' must NOT be redacted as a diagnosis."""
        text = "Update the address and add additional notes"
        out = scrub_lex_phi(text, STAFF)
        assert "address" in out
        assert "additional" in out
        assert "[diagnosis redacted]" not in out
