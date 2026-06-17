"""Tests for phi_guard.scrub_lex_phi -- LEX action-item PHI scrubber (2026-06-14)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.phi_guard import redact_cue_adjacent_names, scrub_lex_phi

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


class TestRedactCueAdjacentNames:
    """B5 cue-proximity + care-noun-governed bare-name redaction (retrieval-only).
    Hardened per the 2026-06-17 adversarial review (both directions)."""

    # --- catches bare client names near a PHI cue (the scrub_lex_phi residual) ---
    def test_name_near_cue_comma_separated(self):
        out = redact_cue_adjacent_names("the client, Madison, missed her session", STAFF)
        assert "Madison" not in out and "[name redacted]" in out

    def test_name_near_incident_cue(self):
        out = redact_cue_adjacent_names("Reviewed the incident involving Jalen Alicea", STAFF)
        assert "Jalen" not in out and "Alicea" not in out

    def test_name_near_appointment_cue(self):
        out = redact_cue_adjacent_names("Lucas Romero missed his appointment again", STAFF)
        assert "Lucas" not in out and "[name redacted]" in out

    # --- review UNDER-redaction fixes ---
    def test_admin_units_cue_redacts_name(self):
        # 'units (of service)' is a PHI cue (care-recipient status via billing).
        out = redact_cue_adjacent_names("Logged 35 units of service for Gabe Mendez.", STAFF)
        assert "Gabe" not in out and "Mendez" not in out

    def test_ahcccs_cue_redacts_name(self):
        out = redact_cue_adjacent_names("Confirmed the AHCCCS number for Lucas Romero.", STAFF)
        assert "Lucas" not in out and "Romero" not in out

    def test_governed_allcaps_name_redacted(self):
        out = redact_cue_adjacent_names("the client, MADISON, missed her session", STAFF)
        assert "MADISON" not in out and "[name redacted]" in out

    def test_governed_accented_name_redacted(self):
        out = redact_cue_adjacent_names("the patient José Doe attended the session", STAFF)
        assert "José" not in out and "Doe" not in out

    def test_client_named_like_staff_first_name_redacted_when_governed(self):
        # 'Aaron' is a staff FIRST name, but the care-noun context marks this an Aaron
        # who is a CLIENT -> redact (context wins over the roster guess).
        out = redact_cue_adjacent_names("the client, Aaron, missed his session", STAFF)
        assert "[name redacted]" in out
        # exact phrase confirms Aaron (not a surrounding word) was the redaction
        assert "client, [name redacted], missed" in out

    # --- review OVER-redaction fixes (legitimate non-PHI must survive) ---
    def test_sentence_initial_verb_near_cue_preserved(self):
        out = redact_cue_adjacent_names("Discussed the client at length today", STAFF)
        assert out == "Discussed the client at length today"

    def test_cue_word_itself_not_redacted(self):
        out = redact_cue_adjacent_names("Client called this morning about the session", STAFF)
        assert out.startswith("Client called")  # the cue word is not a name

    def test_function_word_near_cue_preserved(self):
        out = redact_cue_adjacent_names("The client was present for the session", STAFF)
        assert out == "The client was present for the session"

    def test_staff_nickname_preserved(self):
        # 'Jen' is the roster nickname for staff 'Jennifer Mortensen'.
        out = redact_cue_adjacent_names("Jen reviewed the client's session notes", STAFF)
        assert "Jen reviewed" in out

    def test_staff_full_name_with_trailing_verb_preserved(self):
        # Greedy span 'Shaun Hawkins Reviewed' -> keep the staff prefix + the verb.
        out = redact_cue_adjacent_names("Shaun Hawkins Reviewed the incident report", STAFF)
        assert "Shaun Hawkins" in out and "[name redacted]" not in out

    def test_staff_full_name_near_cue_preserved(self):
        out = redact_cue_adjacent_names("Shaun Hawkins reviewed the client's session", STAFF)
        assert "Shaun Hawkins" in out

    # --- places/words NEAR a cue are still redacted (fail-safe, not an allowlist) ---
    def test_place_near_cue_redacted_failsafe(self):
        out = redact_cue_adjacent_names("Booked the session at Tucson", STAFF)
        assert "Tucson" not in out

    # --- does NOT touch ordinary non-PHI prose (no cue present anywhere) ---
    def test_no_cue_text_untouched(self):
        text = "Met with the family on June 15 about the Tucson site."
        assert redact_cue_adjacent_names(text, STAFF) == text

    # --- documented residuals (access controls are primary) ---
    def test_bare_name_no_cue_is_residual(self):
        # (a) a bare name with NO cue anywhere -> not caught.
        assert redact_cue_adjacent_names("Madison was late.", STAFF) == "Madison was late."

    def test_nongoverned_allcaps_near_cue_is_residual(self):
        # (b) a NON-governed ALLCAPS name near a cue survives (PASS 2 is Title-case;
        # broad ALLCAPS matching is deliberately avoided to not shred acronyms).
        # The GOVERNED ALLCAPS case IS redacted (see test_governed_allcaps_name_redacted).
        out = redact_cue_adjacent_names("Reviewed the session notes. MADISON was late.", STAFF)
        assert "MADISON" in out

    def test_empty_and_none(self):
        assert redact_cue_adjacent_names("", STAFF) == ""
        assert redact_cue_adjacent_names(None, STAFF) is None

    def test_no_redos_on_long_input(self):
        import time
        evil = ("the client " + "Aa " * 6000 + "session")
        start = time.perf_counter()
        redact_cue_adjacent_names(evil, STAFF)
        assert time.perf_counter() - start < 2.0
