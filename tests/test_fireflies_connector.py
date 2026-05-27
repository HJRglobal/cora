"""Unit tests for connectors.fireflies_connector — pure logic helpers.

Tests entity classification, PHI guardrail, and sub-entity tagging.
No network calls — all HTTP functionality mocked or untested here.
"""

import pytest

from cora.connectors.fireflies_connector import (
    _classify_entity,
    _is_phi_meeting,
    _tag_fireflies_sub_entity,
)


# ── _classify_entity ──────────────────────────────────────────────────────────

class TestClassifyEntity:
    @pytest.mark.parametrize("title,expected", [
        ("F3 Energy Q1 Review", "F3E"),
        ("f3e budget planning", "F3E"),
        ("F3 Pure launch recap", "F3E"),
        ("F3 Amazon strategy", "F3E"),
        ("Sprouts sell-in meeting", "F3E"),
        ("Blue Chip Beverages update", "F3E"),
    ])
    def test_f3e_titles(self, title, expected):
        assert _classify_entity(title) == expected

    @pytest.mark.parametrize("title,expected", [
        ("Lexington Services ops sync", "LEX"),
        ("LEX-LLC staff meeting", "LEX"),
        ("LBHS compliance review", "LEX"),
        ("Shaun Hawkins 1:1", "LEX"),
    ])
    def test_lex_titles(self, title, expected):
        assert _classify_entity(title) == expected

    @pytest.mark.parametrize("title,expected", [
        ("OSN Gilbert Warner weekly", "OSN"),
        ("One Stop Nutrition board", "OSN"),
        ("Hayden Greber catchup", "OSN"),
        ("Matt Petrovich quarterly", "OSN"),
    ])
    def test_osn_titles(self, title, expected):
        assert _classify_entity(title) == expected

    @pytest.mark.parametrize("title,expected", [
        ("Big D Media creative sync", "BDM"),
        ("BDM brand shoot debrief", "BDM"),
    ])
    def test_bdm_titles(self, title, expected):
        assert _classify_entity(title) == expected

    @pytest.mark.parametrize("title,expected", [
        ("UFL event planning", "UFL"),
        ("MAS Commercial partnership", "UFL"),
    ])
    def test_ufl_titles(self, title, expected):
        assert _classify_entity(title) == expected

    @pytest.mark.parametrize("title,expected", [
        ("HJR Global finance weekly", "HJRG"),
        ("intercompany allocations review", "HJRG"),
        ("Andrew Stubbs tax planning", "HJRG"),
    ])
    def test_hjrg_titles(self, title, expected):
        assert _classify_entity(title) == expected

    @pytest.mark.parametrize("title,expected", [
        ("HJR Properties Vitalant renewal", "HJRP"),
        ("Cinema Lanes lease negotiation", "HJRP"),
    ])
    def test_hjrp_titles(self, title, expected):
        assert _classify_entity(title) == expected

    @pytest.mark.parametrize("title,expected", [
        ("Chokehold podcast recording", "HJRPROD"),
        ("Falling Forward book session", "HJRPROD"),
        ("HJR Productions Q&A", "HJRPROD"),
    ])
    def test_hjrprod_titles(self, title, expected):
        assert _classify_entity(title) == expected

    @pytest.mark.parametrize("title", [
        "General team sync",
        "All-hands",
        "Quarterly offsite",
        "Miscellaneous planning",
        "",
    ])
    def test_unclassified_defaults_to_fndr(self, title):
        assert _classify_entity(title) == "FNDR"

    def test_case_insensitive(self):
        assert _classify_entity("F3 ENERGY REVIEW") == "F3E"
        assert _classify_entity("osn store ops") == "OSN"

    def test_f3c_title(self):
        assert _classify_entity("F3 Community fundraiser") == "F3C"

    def test_f3_pure_matches_f3e_not_ambiguous(self):
        # "f3 pure" is listed before "f3" in F3E keywords — ensure it routes to F3E
        assert _classify_entity("F3 Pure flavor launch") == "F3E"


# ── _is_phi_meeting ───────────────────────────────────────────────────────────

class TestIsPhiMeeting:
    @pytest.mark.parametrize("title", [
        "treatment plan review",
        "intake assessment",
        "patient intake",
        "consumer record update",
        "clinical assessment",
        "therapy session notes",
        "behavior plan Q3",
        "behavioral plan revision",
        "session note review",
        "case conference",
    ])
    def test_lex_phi_titles_excluded(self, title):
        assert _is_phi_meeting(title, "LEX") is True

    def test_non_lex_entity_never_phi(self):
        assert _is_phi_meeting("treatment plan review", "F3E") is False
        assert _is_phi_meeting("patient intake", "OSN") is False

    @pytest.mark.parametrize("title", [
        "LEX-LLC staff sync",
        "Lexington Services compliance",
        "LEX ops scheduling",
        "LLA quarterly review",
    ])
    def test_non_phi_lex_titles_pass(self, title):
        assert _is_phi_meeting(title, "LEX") is False

    def test_case_insensitive(self):
        assert _is_phi_meeting("Treatment Plan Review", "LEX") is True


# ── _tag_fireflies_sub_entity ─────────────────────────────────────────────────

class TestTagFirefliesSubEntity:
    def _make_transcript(self, attendees: list[dict]) -> dict:
        return {"meeting_attendees": attendees}

    def _attendee(self, display_name: str, email: str) -> dict:
        return {"displayName": display_name, "email": email}

    def test_justin_gilmore_returns_lts(self):
        t = self._make_transcript([self._attendee("Justin Gilmore", "justin.gilmore@lex.com")])
        assert _tag_fireflies_sub_entity(t) == "LEX-LTS"

    def test_jared_harker_returns_lbhs(self):
        t = self._make_transcript([self._attendee("Jared Harker", "jared.harker@lex.com")])
        assert _tag_fireflies_sub_entity(t) == "LEX-LBHS"

    def test_sandy_patel_returns_lla(self):
        t = self._make_transcript([self._attendee("Sandy Patel", "sandy.patel@lex.com")])
        assert _tag_fireflies_sub_entity(t) == "LEX-LLA"

    def test_shaun_hawkins_returns_llc(self):
        t = self._make_transcript([self._attendee("Shaun Hawkins", "shaun.hawkins@lex.com")])
        assert _tag_fireflies_sub_entity(t) == "LEX-LLC"

    def test_multiple_sub_entities_returns_none(self):
        # Both LTS and LBHS present → cross-sub-entity meeting → None
        t = self._make_transcript([
            self._attendee("Justin Gilmore", "justin.gilmore@lex.com"),
            self._attendee("Jared Harker", "jared.harker@lex.com"),
        ])
        assert _tag_fireflies_sub_entity(t) is None

    def test_no_sub_entity_signals_returns_none(self):
        t = self._make_transcript([self._attendee("Harrison Rogers", "harrison@hjrglobal.com")])
        assert _tag_fireflies_sub_entity(t) is None

    def test_empty_attendees_returns_none(self):
        assert _tag_fireflies_sub_entity({"meeting_attendees": []}) is None

    def test_missing_attendees_key_returns_none(self):
        assert _tag_fireflies_sub_entity({}) is None
