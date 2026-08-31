"""F3E blog preflight false-positive fixes (session #11 S7, cq-85b35413b020 HIGH).

SCOPE IS DELIBERATELY NARROW. The seed names two FPs and asks for the rail-2
"clean near a brand line" semantics to be attribution-scoped. That change would
REVERSE an explicit, twice-documented design decision -- pipeline.py: "Loosening
the rail instead would have traded a productivity problem for a claims hole", and
drafting.py names the exact sentence as forbidden and teaches the model to split
it. Reversing a claims rail on beverage copy is a regulatory judgment, not a code
judgment, so it is put to Harrison rather than taken here.

Fixed here are the two classes NO existing design note contests:
  R2 -- a Title-Case proper noun headed by "Clean" ("the Clean Label Project")
  R1 -- a disclaimer whose claim verb sits outside the 1-3 space negation window

Both directions are pinned. A rail that stops catching real claims is far worse
than one that over-catches, so every true positive below must keep tripping.
"""
from __future__ import annotations

import time

import pytest

from src.cora.f3e_blog.preflight import run_preflight


def _trips(text: str) -> list[str]:
    result = run_preflight(title="Post", summary="", body_html="<p>%s</p>" % text)
    return sorted({t.rail_id for t in result.trips})


class TestR2ProperNounIsNotAClaim:
    def test_clean_label_project_is_clean(self):
        assert _trips("F3 Energy works with the Clean Label Project.") == []

    def test_other_proper_nouns_headed_by_clean(self):
        assert _trips("F3 Energy joined the Cleaner Future Alliance.") == []

    def test_the_adjective_still_trips(self):
        assert "R2" in _trips("F3 Energy is all-natural and clean.")

    def test_redaction_does_not_blind_the_rest_of_the_sentence(self):
        """The exemption is a REDACTION, so the remainder is still scanned --
        a proper noun must not become a shield for a real claim beside it."""
        assert "R2" in _trips(
            "F3 Energy works with the Clean Label Project and is all-natural.")

    def test_the_evasion_pin_still_holds(self):
        """brand_lines_in's names_f3 override is load-bearing for this case."""
        assert "R2" in _trips("Energy drinks from F3 are all-natural and clean.")


class TestR1DisclaimerScope:
    def test_the_named_false_positive_is_clean(self):
        assert _trips("F3 Energy is not about eliminating jitters or preventing a crash.") == []

    def test_ordinary_disclaimer_shapes_still_clear(self):
        for text in (
            "F3 Energy is not intended to treat any condition.",
            "This product does not cure anything.",
            "F3 is never a treatment for illness.",
        ):
            assert _trips(text) == [], text

    def test_a_real_claim_still_trips(self):
        assert "R1" in _trips("F3 Energy helps prevent migraines.")

    def test_scope_cannot_bridge_a_dash_into_a_real_claim(self):
        """The negated class excludes dashes, so a negation in one clause cannot
        clear a claim in the next."""
        assert "R1" in _trips("We are not shy -- F3 Energy prevents migraines.")

    def test_scope_cannot_bridge_a_comma_into_a_real_claim(self):
        assert "R1" in _trips("This is not a drug, it cures hangovers.")

    def test_disease_object_still_trips_without_a_verb(self):
        assert "R1" in _trips("F3 Energy supports your immune system.")


class TestNoRedos:
    """Seven ReDoS bugs have been found in this repo. Both new patterns are
    bounded -- one lazy negated class, one bounded repetition."""

    @pytest.mark.parametrize("payload", [
        "F3 Energy is not " + "a " * 4000 + "prevent",
        "F3 Energy works with the Clean " + "Label " * 4000,
        "not" + " " * 5000 + "treat",
    ])
    def test_pathological_input_returns_promptly(self, payload):
        start = time.time()
        run_preflight(title="Post", summary="", body_html="<p>%s</p>" % payload)
        assert time.time() - start < 5.0


class TestBenignControl:
    def test_ordinary_copy_is_clean(self):
        assert _trips("F3 Energy launched a new flavor this week.") == []
