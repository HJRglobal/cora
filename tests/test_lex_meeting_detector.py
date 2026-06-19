"""WS2: shared LEX meeting detector + KB hard-exclude.

The detector (fireflies_connector.classify_lex_meeting) is the SINGLE source of
truth used by both the KB-ingest path and the meeting-capture pull tool. Root
case it must catch: a Lexington probation "1st Budget Class" (organizer
@hjrglobal.com, *.maricopa.gov clients, generic title) -> LEX + hard-exclude.
"""

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
