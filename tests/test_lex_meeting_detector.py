"""WS2: shared LEX meeting detector + KB hard-exclude.

The detector (fireflies_connector.classify_lex_meeting) is the SINGLE source of
truth used by both the KB-ingest path and the meeting-capture pull tool. Root
case it must catch: a Lexington probation "1st Budget Class" (organizer
@hjrglobal.com, *.maricopa.gov clients, generic title) -> LEX + hard-exclude.
"""

from unittest.mock import patch

from cora.connectors import fireflies_connector as fc


def _t(title="", attendees=None, organizer="", participants=None):
    return {
        "title": title,
        "organizer_email": organizer,
        "meeting_attendees": [
            {"displayName": d, "email": e} for d, e in (attendees or [])
        ],
        "participants": participants or [],
    }


BUDGET_CLASS = _t(
    title="1st Budget Class",
    organizer="alina@hjrglobal.com",
    attendees=[("PO Smith", "psmith@mail.maricopa.gov"), ("Client A", "clienta@gmail.com")],
)


class TestClassifyLexMeeting:
    def test_budget_class_is_lex_and_hard_excluded(self):
        v = fc.classify_lex_meeting(BUDGET_CLASS)
        assert v.is_lex is True
        assert v.hard_exclude_kb is True
        assert v.sub_entity == "LEX"   # no specific sub-entity signal -> GM-level

    def test_lex_ops_meeting_ingests_lex_scoped(self):
        v = fc.classify_lex_meeting(_t(
            title="Lexington Services Weekly Ops Sync",
            attendees=[("Shaun Hawkins", "shaun@lexingtonservices.com")],
        ))
        assert v.is_lex is True
        assert v.hard_exclude_kb is False        # plain ops -> still ingested
        assert v.sub_entity == "LEX-LLC"         # Shaun -> LLC (named lead)

    def test_lbhs_domain_hard_excluded(self):
        v = fc.classify_lex_meeting(_t(
            title="Team Check-in",
            attendees=[("X", "x@lexingtonbhs.com")],
        ))
        assert v.is_lex is True
        assert v.sub_entity == "LEX-LBHS"
        assert v.hard_exclude_kb is True         # 42 CFR Part 2

    def test_ddd_title_hard_excluded(self):
        v = fc.classify_lex_meeting(_t(title="DDD ISP Meeting"))
        assert v.is_lex is True
        assert v.hard_exclude_kb is True

    def test_organizer_plus_gov_generic_title_is_lex(self):
        # No program keyword, no lex-domain attendee -- but a known LEX-program
        # organizer hosting government clients -> LEX program, hard-excluded.
        v = fc.classify_lex_meeting(_t(
            title="Weekly Check-in",
            organizer="alina@hjrglobal.com",
            attendees=[("PO", "po@maricopa.gov")],
        ))
        assert v.is_lex is True
        assert v.hard_exclude_kb is True

    def test_f3e_meeting_not_lex(self):
        v = fc.classify_lex_meeting(_t(
            title="F3 Energy Retail Sync",
            attendees=[("Tommy", "tommy@f3energy.com")],
        ))
        assert v.is_lex is False
        assert v.hard_exclude_kb is False

    def test_gov_attendee_without_corroboration_not_lex(self):
        # A lone .gov attendee, no LEX-program organizer, no other LEX signal:
        # must NOT be mis-classified as LEX (e.g. an HJRG regulatory call).
        v = fc.classify_lex_meeting(_t(
            title="Regulatory Call",
            organizer="harrison@hjrglobal.com",
            attendees=[("Reg", "reg@azdes.gov")],
        ))
        assert v.is_lex is False

    def test_generic_meeting_not_lex(self):
        v = fc.classify_lex_meeting(_t(
            title="Team Standup",
            attendees=[("Harrison", "harrison@hjrglobal.com")],
        ))
        assert v.is_lex is False

    def test_f3_budget_review_not_misrouted_to_lex(self):
        # "f3 budget" is an F3E title keyword; "budget class" is the LEX pattern.
        # "F3 Budget Review" must NOT trip the LEX program pattern.
        v = fc.classify_lex_meeting(_t(title="F3 Budget Review"))
        assert v.is_lex is False


class TestConfigDefaults:
    def test_defaults_present(self):
        cfg = fc._load_lex_detect_cfg()
        assert "budget class" in cfg["program_titles"]
        assert "alina@hjrglobal.com" in cfg["organizers"]
        assert ".gov" in cfg["client_domain_suffixes"]


class TestMeetingActionsDelegation:
    def test_classify_meeting_budget_class_is_lex(self):
        from cora.tools import meeting_actions as ma
        entity, is_lex = ma._classify_meeting(BUDGET_CLASS)
        assert (entity, is_lex) == ("LEX", True)

    def test_scope_subentity_budget_class(self):
        from cora.tools import meeting_actions as ma
        assert ma._lex_scope_subentity(BUDGET_CLASS) == "LEX"

    def test_classify_meeting_non_lex(self):
        from cora.tools import meeting_actions as ma
        entity, is_lex = ma._classify_meeting(_t(
            title="F3 Energy Retail Sync",
            attendees=[("Tommy", "tommy@f3energy.com")],
        ))
        assert is_lex is False
        assert entity == "F3E"


class TestAmbiguousProgramTitles:
    """Review fix #3: a business-AMBIGUOUS program title alone must NOT classify a
    non-LEX meeting as LEX (and silently hard-exclude it from the KB)."""

    def test_program_title_without_corroboration_not_lex(self):
        for title in (
            "F3 Financial Class Q3",
            "Day Program Marketing Sync",
            "Podcast: Financial Literacy for Founders",
            "Independent Living Content Plan",
        ):
            v = fc.classify_lex_meeting(_t(title=title, attendees=[("X", "x@f3energy.com")]))
            assert v.is_lex is False, title
            assert v.hard_exclude_kb is False, title

    def test_program_title_corroborated_by_lex_domain_is_lex(self):
        v = fc.classify_lex_meeting(_t(
            title="1st Budget Class",
            attendees=[("Jen", "jen@lexingtonservices.com")],
        ))
        assert v.is_lex is True and v.hard_exclude_kb is True

    def test_lex_substring_word_boundary(self):
        # "lex-" must match at a word boundary only, not inside Duplex-/Complex-.
        assert fc.classify_lex_meeting(_t(title="Complex Pricing Review")).is_lex is False
        assert fc.classify_lex_meeting(_t(title="Duplex-ready Listing Prep")).is_lex is False

    def test_ddd_title_still_self_sufficient(self):
        # DDD is LEX/healthcare-specific -> stays self-sufficient (no corroboration).
        v = fc.classify_lex_meeting(_t(title="DDD ISP Meeting"))
        assert v.is_lex is True and v.hard_exclude_kb is True

    def test_care_titles_self_sufficient_without_corroboration(self):
        # Review-2 fix: LEX care/clinical-program titles are caught even with a
        # NON-allowlisted @hjrglobal.com organizer + private-email-only clients (no .gov).
        for title in ("Day Treatment Session", "Anger Management Group", "HCBS Planning"):
            v = fc.classify_lex_meeting(_t(
                title=title, organizer="casey@hjrglobal.com",
                attendees=[("Client", "client@gmail.com")],
            ))
            assert v.is_lex is True and v.hard_exclude_kb is True, title


class TestConfigRobustness:
    """Review fix #4: malformed lex-scope YAML must degrade to defaults, never crash
    or char-iterate a bare string into single-letter patterns."""

    def _reload_with(self, tmp_path, content):
        p = tmp_path / "scope.yaml"
        p.write_text(content, encoding="utf-8")
        fc._lex_detect_cfg = None
        try:
            with patch.object(fc, "_LEX_DETECT_CFG_PATH", p):
                return fc._load_lex_detect_cfg()
        finally:
            fc._lex_detect_cfg = None  # let other tests reload the real file

    def test_non_dict_yaml_falls_back_to_defaults(self, tmp_path):
        cfg = self._reload_with(tmp_path, "just a bare string\n")
        assert "budget class" in cfg["program_titles"]
        assert "alina@hjrglobal.com" in cfg["organizers"]

    def test_per_key_string_not_char_iterated(self, tmp_path):
        cfg = self._reload_with(tmp_path, 'lex_program_title_patterns: "town hall"\n')
        assert "town hall" in cfg["program_titles"]   # added as ONE pattern
        assert "t" not in cfg["program_titles"]        # NOT split into characters
        assert "budget class" in cfg["program_titles"] # defaults preserved


class TestBackfillHardExcludeWiring:
    """Review fix #6: exercise backfill()'s actual ingest-prevention wiring, not just
    the verdict -- a LEX program/client meeting must never be yielded for KB ingest."""

    def _t(self, tid, title, link, attendees, organizer=""):
        return {
            "id": tid, "title": title, "date": 1_780_000_000, "meeting_link": link,
            "duration": 1800, "organizer_email": organizer,
            "summary": {"overview": "ov", "action_items": "do x"},
            "sentences": [{"index": i, "speaker_name": "A", "text": f"l{i}"} for i in range(4)],
            "meeting_attendees": attendees,
        }

    def test_program_meeting_excluded_ops_meeting_ingested(self, tmp_path):
        from datetime import datetime, timezone
        bc = self._t("BC", "1st Budget Class", "Lbc",
                     [{"displayName": "PO", "email": "po@maricopa.gov"}], "alina@hjrglobal.com")
        ops = self._t("OPS", "Lexington Services Ops Sync", "Lops",
                      [{"displayName": "Shaun Hawkins", "email": "shaun@lexingtonservices.com"}])
        osn = self._t("OSN1", "OSN Store Inventory", "Losn",
                      [{"displayName": "Matt", "email": "matt@onestopnutrition.com"}])
        with (
            patch.object(fc, "_DEDUP_LEDGER_PATH", tmp_path / "led.json"),
            patch.object(fc, "_graphql_query", return_value={"transcripts": [bc, ops, osn]}),
        ):
            docs = list(fc.backfill(datetime(2020, 1, 1, tzinfo=timezone.utc)))
        ids = {d.source_id for d in docs}
        assert "BC" not in ids               # LEX program/client meeting hard-excluded
        assert "OPS" in ids and "OSN1" in ids
        ent = {d.source_id: d.entity for d in docs}
        assert ent["OPS"] == "LEX"           # plain LEX ops still ingests LEX-scoped
