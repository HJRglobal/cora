"""Tests for the PULL meeting-action-items flow (cora.tools.meeting_actions).

Covers: transcript disambiguation/pick-list, asker-item filtering, the
attendee gate (preview + confirm), channel/DM scope gate (incl. the
_channel_name=="dm" DM signal), the D-052 LEX rails (scrub, LEX-only scope,
LBHS exclusion, clinical-title skip, LEX-only project routing, AND the
participant-email LEX detector for generically-titled LEX meetings), the
pick-list scope filter (no cross-entity/LEX title leak), the staged-write
preview/confirm protocol with content validation, and a guard that the
recall/ingest path is undisturbed.

Mocking convention follows test_meeting_action_capture.py + the repo test-patch
doctrine: patch directly-imported names ON the importing module
(patch.object(meeting_actions, ...)).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.tools import meeting_actions as ma  # noqa: E402
from cora.connectors import fireflies_action_extractor as fae  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ASKER = "U_ASKER"
ASKER_EMAIL = "asker@hjrglobal.com"
ASKER_NAME = "Tommy Anderson"


def _mk_transcript(
    *,
    tid="01TID",
    title="F3 Marketing Sync",
    date_ms=1_750_000_000_000,
    meeting_link="https://meet.example/x",
    attendees=None,
    action_items="**Tommy Anderson**\nSend the deck (Fri)\n",
    short_summary="We talked about the launch.",
):
    if attendees is None:
        attendees = [{"displayName": None, "email": ASKER_EMAIL}]
    return {
        "id": tid,
        "title": title,
        "date": date_ms,
        "meeting_link": meeting_link,
        "participants": [a.get("email") for a in attendees if a.get("email")],
        "summary": {
            "overview": "ov",
            "short_summary": short_summary,
            "action_items": action_items,
        },
        "meeting_attendees": attendees,
    }


@pytest.fixture(autouse=True)
def _reset_cache():
    ma._module_slack_map = None
    yield
    ma._module_slack_map = None


@pytest.fixture
def asker_identity():
    """Patch the asker's identity helpers to a known F3E user."""
    slack_map = {
        ASKER: {
            "slack_user_id": ASKER,
            "asana_user_gid": "GID_ASKER",
            "asana_email": ASKER_EMAIL,
            "display_name": ASKER_NAME,
        }
    }
    with (
        patch.object(ma, "_load_slack_map", return_value=slack_map),
        patch.object(ma.org_roles, "get_role", return_value=None),
        patch.object(fae, "_roster_names", return_value=[ASKER_NAME, "Harrison Rogers", "Larry Stone"]),
    ):
        yield


def _input(**kw):
    base = {"_channel_id": "C_F3E"}  # default: a channel (not a DM)
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------

class TestScopeGate:
    def test_lex_meeting_allowed_in_lex_channel(self):
        ok, _ = ma._scope_ok("LEX", "LEX-LLC", is_dm=False)
        assert ok

    def test_lex_meeting_refused_in_non_lex_channel(self):
        ok, reason = ma._scope_ok("LEX", "F3E", is_dm=False)
        assert not ok and "Lexington" in reason

    def test_lex_meeting_refused_in_non_lex_dm(self):
        ok, _ = ma._scope_ok("LEX", "F3E", is_dm=True)
        assert not ok

    def test_lex_meeting_allowed_in_lex_dm(self):
        ok, _ = ma._scope_ok("LEX", "LEX-LLC", is_dm=True)
        assert ok

    def test_nonlex_allowed_same_entity_channel(self):
        assert ma._scope_ok("F3E", "F3E", is_dm=False)[0]

    def test_nonlex_allowed_subentity_channel(self):
        assert ma._scope_ok("OSN", "OSNGW", is_dm=False)[0]

    def test_nonlex_allowed_aggregator_channel(self):
        assert ma._scope_ok("F3E", "FNDR", is_dm=False)[0]
        assert ma._scope_ok("OSN", "HJRG", is_dm=False)[0]

    def test_nonlex_allowed_in_any_dm(self):
        assert ma._scope_ok("OSN", "F3E", is_dm=True)[0]

    def test_nonlex_refused_cross_entity_channel(self):
        ok, reason = ma._scope_ok("OSN", "F3E", is_dm=False)
        assert not ok and "OSN" in reason


class TestLexGate:
    def test_non_lex_passthrough(self):
        ok, reason, scoped = ma._lex_gate(_mk_transcript(), "F3 Sync", "F3E")
        assert ok and reason == "" and scoped == ""

    def test_lex_disabled_refuses(self):
        with patch.object(fae, "_lex_capture_enabled", return_value=False):
            ok, reason, _ = ma._lex_gate(_mk_transcript(), "Lex Sync", "LEX")
        assert not ok and "turned off" in reason

    def test_lbhs_excluded(self):
        with (
            patch.object(fae, "_lex_capture_enabled", return_value=True),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value="LEX-LBHS"),
            patch.object(fae, "_lex_sub_entity_allowed", return_value=False),
        ):
            ok, reason, scoped = ma._lex_gate(_mk_transcript(), "Lex Sync", "LEX")
        assert not ok and scoped == "LEX-LBHS"

    def test_clinical_title_skipped(self):
        with (
            patch.object(fae, "_lex_capture_enabled", return_value=True),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value="LEX-LLC"),
            patch.object(fae, "_lex_sub_entity_allowed", return_value=True),
            patch.object(ma, "_is_phi_meeting", return_value=True),
        ):
            ok, reason, _ = ma._lex_gate(_mk_transcript(), "Treatment Plan Review", "LEX")
        assert not ok and "clinical" in reason

    def test_lex_operational_allowed(self):
        with (
            patch.object(fae, "_lex_capture_enabled", return_value=True),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value="LEX-LLC"),
            patch.object(fae, "_lex_sub_entity_allowed", return_value=True),
            patch.object(ma, "_is_phi_meeting", return_value=False),
        ):
            ok, reason, scoped = ma._lex_gate(_mk_transcript(), "LLC Ops Sync", "LEX")
        assert ok and scoped == "LEX-LLC"


class TestClassifyMeeting:
    def test_title_classified_lex(self):
        with (
            patch.object(ma, "_classify_entity", return_value="LEX"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value=None),
        ):
            ent, is_lex = ma._classify_meeting(_mk_transcript(title="Lexington Sync"))
        assert ent == "LEX" and is_lex

    def test_participant_detected_lex_despite_generic_title(self):
        # FIX (HIGH): title classifies non-LEX, but a LEX attendee is present.
        with (
            patch.object(ma, "_classify_entity", return_value="FNDR"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value="LEX-LLC"),
        ):
            ent, is_lex = ma._classify_meeting(_mk_transcript(title="Tuesday Sync"))
        assert ent == "LEX" and is_lex

    def test_non_lex(self):
        with (
            patch.object(ma, "_classify_entity", return_value="F3E"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value=None),
        ):
            ent, is_lex = ma._classify_meeting(_mk_transcript())
        assert ent == "F3E" and not is_lex

    def test_email_domain_detects_lex_without_named_lead(self):
        # FIX (MEDIUM, 2nd review): generic title + no named lead, but a Lexington
        # email-domain attendee (Jen) -> still LEX.
        t = _mk_transcript(
            title="Tuesday Sync",
            attendees=[{"displayName": "Jen", "email": "jen@lexingtonservices.com"}],
        )
        with (
            patch.object(ma, "_classify_entity", return_value="FNDR"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value=None),
        ):
            ent, is_lex = ma._classify_meeting(t)
        assert ent == "LEX" and is_lex

    def test_lbhs_email_domain_wins_for_exclusion(self):
        # An @lexingtonbhs.com attendee forces LEX-LBHS (Part 2) even with no
        # named lead -- so the gate excludes it.
        t = _mk_transcript(
            title="Tuesday Sync",
            attendees=[{"displayName": "x", "email": "staff@lexingtonbhs.com"}],
        )
        with patch.object(ma, "_tag_fireflies_sub_entity", return_value=None):
            assert ma._lex_scope_subentity(t) == "LEX-LBHS"

    def test_lex_domain_gm_default(self):
        t = _mk_transcript(attendees=[{"displayName": "Jen", "email": "jen@lexingtonservices.com"}])
        with patch.object(ma, "_tag_fireflies_sub_entity", return_value=None):
            assert ma._lex_scope_subentity(t) == "LEX"


class TestSplitCandidates:
    def test_mine_vs_unclear_vs_others(self):
        roster = [ASKER_NAME, "Harrison Rogers", "Larry Stone"]
        items = [
            {"task": "Send the deck", "assignee_name": "Tommy Anderson", "due_mention": "Fri"},
            {"task": "Unowned item", "assignee_name": None, "due_mention": None},
            {"task": "Harrison's job", "assignee_name": "Harrison Rogers", "due_mention": None},
        ]
        mine, unclear = ma._split_candidates(items, ASKER_NAME, roster)
        assert [m["task"] for m in mine] == ["Send the deck"]
        assert [u["task"] for u in unclear] == ["Unowned item"]

    def test_nickname_matches_asker(self):
        roster = ["Jennifer Mortensen", "Harrison Rogers"]
        items = [{"task": "do X", "assignee_name": "Jen", "due_mention": None}]
        mine, _ = ma._split_candidates(items, "Jennifer Mortensen", roster)
        assert len(mine) == 1

    def test_off_roster_named_goes_to_unclear(self):
        # FIX (NIT, 2nd review): a named-but-off-roster owner (vendor/mis-parse)
        # is claimable (unclear), matching the docstring -- not silently dropped.
        roster = [ASKER_NAME, "Harrison Rogers"]
        items = [{"task": "ship samples", "assignee_name": "Dennis Morales", "due_mention": None}]
        mine, unclear = ma._split_candidates(items, ASKER_NAME, roster)
        assert mine == [] and [u["task"] for u in unclear] == ["ship samples"]


class TestDedupAndMatch:
    def test_dedup_collapses_same_meeting_same_day(self):
        t1 = _mk_transcript(tid="A", action_items="x")
        t2 = _mk_transcript(tid="B", action_items="x\ny\nz longer")  # same link+title+day, more complete
        kept = ma._dedup_meetings([t1, t2])
        assert len(kept) == 1 and kept[0]["id"] == "B"

    def test_dedup_keeps_distinct_links_same_title_day(self):
        # FIX (LOW): two genuinely-different meetings sharing a title on the same
        # day but with different meeting_links stay separately selectable.
        t1 = _mk_transcript(tid="A", title="Standup", meeting_link="https://meet/a")
        t2 = _mk_transcript(tid="B", title="Standup", meeting_link="https://meet/b")
        kept = ma._dedup_meetings([t1, t2])
        assert len(kept) == 2

    def test_match_substring(self):
        ts = [_mk_transcript(title="F3 Marketing Sync"), _mk_transcript(title="OSN Weekly")]
        assert len(ma._match_query("marketing", ts)) == 1

    def test_match_tokens(self):
        ts = [_mk_transcript(title="Weekly F3 Marketing Standup")]
        assert len(ma._match_query("marketing weekly", ts)) == 1

    def test_match_none(self):
        ts = [_mk_transcript(title="F3 Marketing Sync")]
        assert ma._match_query("budget class", ts) == []


class TestAttendeeGate:
    def test_attended_via_email_fallback(self):
        t = _mk_transcript(attendees=[{"displayName": None, "email": ASKER_EMAIL}])
        with patch.object(ma, "_resolve_participant_slack_ids", return_value=[]):
            assert ma._asker_attended(t, {ASKER_EMAIL}, ASKER)

    def test_attended_via_slack_resolution(self):
        t = _mk_transcript(attendees=[{"displayName": None, "email": "x@y.com"}])
        with patch.object(ma, "_resolve_participant_slack_ids", return_value=[ASKER]):
            assert ma._asker_attended(t, set(), ASKER)

    def test_not_attended(self):
        t = _mk_transcript(attendees=[{"displayName": None, "email": "someone@else.com"}])
        with patch.object(ma, "_resolve_participant_slack_ids", return_value=["U_OTHER"]):
            assert not ma._asker_attended(t, {ASKER_EMAIL}, ASKER)


class TestItemMatchesMeeting:
    def test_matches_on_shared_tokens(self):
        assert ma._item_matches_meeting("Follow up re billing", "billing authorization due")

    def test_blocks_fabricated_text(self):
        assert not ma._item_matches_meeting("buy a yacht in monaco", "send the proposal deck")

    def test_fail_open_on_empty_meeting_text(self):
        assert ma._item_matches_meeting("anything", "")

    def test_fail_open_on_short_item(self):
        assert ma._item_matches_meeting("ok", "send the proposal deck")


# ---------------------------------------------------------------------------
# Preview / resolve (read-only) — entry point
# ---------------------------------------------------------------------------

class TestPreview:
    def test_unmapped_asker_refused(self):
        with patch.object(ma, "_asker_emails", return_value=set()):
            out = ma.run_meeting_action_items("U_NOBODY", "F3E", _input(meeting_query="x"))
        assert "Slack-to-Asana map" in out

    def test_pick_list_on_multiple_matches(self, asker_identity):
        t1 = _mk_transcript(tid="A", title="F3 Marketing Sync", meeting_link="https://m/a", date_ms=1_750_000_000_000)
        t2 = _mk_transcript(tid="B", title="F3 Marketing Strategy", meeting_link="https://m/b", date_ms=1_749_000_000_000)
        with (
            patch.object(ma, "_recent_transcripts", return_value=[t1, t2]),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="F3E"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value=None),
        ):
            out = ma.run_meeting_action_items(ASKER, "F3E", _input(meeting_query="f3 marketing"))
        assert "which one" in out.lower()
        assert "[id:A]" in out and "[id:B]" in out

    def test_recent_list_when_no_query(self, asker_identity):
        t1 = _mk_transcript(tid="A", title="F3 Marketing Sync")
        with (
            patch.object(ma, "_recent_transcripts", return_value=[t1]),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="F3E"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value=None),
        ):
            out = ma.run_meeting_action_items(ASKER, "F3E", _input())
        assert "[id:A]" in out and "Recent meetings" in out

    def test_recent_list_excludes_lex_meeting_in_f3e_channel(self, asker_identity):
        # FIX (HIGH): the pick-list/recent-list must not enumerate a LEX (or
        # cross-entity) meeting's title/existence into a non-LEX channel.
        f3e = _mk_transcript(tid="F3", title="F3 Sync", meeting_link="https://m/f")
        lex = _mk_transcript(tid="LX", title="Lex Care Sync", meeting_link="https://m/l")
        with (
            patch.object(ma, "_recent_transcripts", return_value=[f3e, lex]),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", side_effect=lambda t: "LEX" if "lex" in t.lower() else "F3E"),
            patch.object(ma, "_tag_fireflies_sub_entity", side_effect=lambda tr: "LEX-LLC" if "lex" in (tr.get("title") or "").lower() else None),
            patch.object(fae, "_lex_capture_enabled", return_value=True),
            patch.object(fae, "_lex_sub_entity_allowed", return_value=True),
            patch.object(ma, "_is_phi_meeting", return_value=False),
        ):
            out = ma.run_meeting_action_items(ASKER, "F3E", _input())
        assert "[id:F3]" in out          # F3E meeting shown
        assert "[id:LX]" not in out       # LEX meeting filtered out of the F3E channel
        assert "Lex Care Sync" not in out

    def test_not_found(self, asker_identity):
        with (
            patch.object(ma, "_recent_transcripts", return_value=[_mk_transcript()]),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="F3E"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value=None),
        ):
            out = ma.run_meeting_action_items(ASKER, "F3E", _input(meeting_query="nonexistent topic"))
        assert "couldn't find" in out

    def test_single_match_preview_shows_my_items(self, asker_identity):
        t = _mk_transcript(title="F3 Marketing Sync")
        parsed = [
            {"task": "Send the deck", "assignee_name": ASKER_NAME, "due_mention": "Fri"},
            {"task": "Larry's item", "assignee_name": "Larry Stone", "due_mention": None},
            {"task": "Unowned thing", "assignee_name": None, "due_mention": None},
        ]
        with (
            patch.object(ma, "_recent_transcripts", return_value=[t]),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="F3E"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value=None),
            patch.object(fae, "_parse_action_items_with_haiku", return_value=parsed),
        ):
            out = ma.run_meeting_action_items(ASKER, "F3E", _input(meeting_query="marketing"))
        assert "Send the deck" in out          # mine
        assert "Unowned thing" in out          # unclear (claimable)
        assert "Larry's item" not in out       # someone else's -> excluded
        assert "transcript_id=\"01TID\"" in out
        assert "confirmed=true" in out

    def test_non_attendee_refused_at_preview(self, asker_identity):
        t = _mk_transcript()
        with (
            patch.object(ma, "_recent_transcripts", return_value=[t]),
            patch.object(ma, "_asker_attended", return_value=False),
        ):
            out = ma.run_meeting_action_items(ASKER, "F3E", _input(meeting_query="marketing"))
        assert "couldn't find" in out  # filtered out by attendance; never surfaced

    def test_transcript_id_bypass_still_checks_attendance(self, asker_identity):
        t = _mk_transcript(tid="DIRECT")
        with (
            patch.object(ma, "_fetch_transcript_by_id", return_value=t),
            patch.object(ma, "_asker_attended", return_value=False),
        ):
            out = ma.run_meeting_action_items(ASKER, "F3E", _input(transcript_id="DIRECT"))
        assert "meetings you attended" in out

    def test_transcript_id_not_found_message(self, asker_identity):
        # FIX (NIT): a direct transcript_id that can't be loaded gives a
        # transcript-specific message, not 'matching ""'.
        with patch.object(ma, "_fetch_transcript_by_id", return_value=None):
            out = ma.run_meeting_action_items(ASKER, "F3E", _input(transcript_id="GONE"))
        assert "couldn't load that meeting" in out

    def test_lex_meeting_refused_via_direct_id_in_f3e_channel(self, asker_identity):
        # transcript_id-direct bypasses the visible-filter, so the single-match
        # scope gate must still refuse a LEX meeting in a non-LEX channel.
        t = _mk_transcript(tid="LX", title="Lexington LLC Ops Sync")
        with (
            patch.object(ma, "_fetch_transcript_by_id", return_value=t),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="LEX"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value="LEX-LLC"),
        ):
            out = ma.run_meeting_action_items(ASKER, "F3E", _input(transcript_id="LX"))
        assert "Lexington" in out

    def test_generic_title_lex_refused_via_participant_detector(self, asker_identity):
        # FIX (HIGH): a generically-titled meeting with a NAMED LEX lead is treated
        # as LEX and refused in a non-LEX channel (title classified FNDR).
        t = _mk_transcript(tid="GEN", title="Tuesday Sync")
        with (
            patch.object(ma, "_fetch_transcript_by_id", return_value=t),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="FNDR"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value="LEX-LLC"),
        ):
            out = ma.run_meeting_action_items(ASKER, "F3E", _input(transcript_id="GEN"))
        assert "Lexington" in out

    def test_generic_title_lex_refused_via_email_domain(self, asker_identity):
        # FIX (MEDIUM, 2nd review): generic title, NO named lead, but a Lexington
        # email-domain attendee -> treated as LEX, refused in a non-LEX channel.
        t = _mk_transcript(
            tid="DOM", title="Tuesday Sync",
            attendees=[
                {"displayName": None, "email": ASKER_EMAIL},
                {"displayName": "Jen", "email": "jen@lexingtonservices.com"},
            ],
        )
        with (
            patch.object(ma, "_fetch_transcript_by_id", return_value=t),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="FNDR"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value=None),
        ):
            out = ma.run_meeting_action_items(ASKER, "F3E", _input(transcript_id="DOM"))
        assert "Lexington" in out

    def test_dm_signal_via_channel_name(self, asker_identity):
        # FIX (CRITICAL): is_dm derived from _channel_name=="dm" (channel_id is
        # NOT threaded into the QA tool loop). A non-LEX meeting is allowed in a
        # DM even when entity != meeting entity.
        t = _mk_transcript(tid="OSN1", title="OSN Weekly")
        with (
            patch.object(ma, "_fetch_transcript_by_id", return_value=t),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="OSN"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value=None),
            patch.object(fae, "_parse_action_items_with_haiku", return_value=[]),
        ):
            # entity is the asker's primary (F3E), but it's a DM -> allowed.
            out = ma.run_meeting_action_items(
                ASKER, "F3E", {"_channel_name": "dm", "transcript_id": "OSN1"}
            )
        assert "scoped to" not in out  # NOT refused
        assert "MEETING:" in out

    def test_lex_meeting_preview_scrubbed_in_lex_channel(self, asker_identity):
        t = _mk_transcript(title="LLC Ops Sync", short_summary="client John Doe DOB 1/1/90")
        parsed = [{"task": "Follow up re client", "assignee_name": ASKER_NAME, "due_mention": None}]
        with (
            patch.object(ma, "_recent_transcripts", return_value=[t]),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="LEX"),
            patch.object(fae, "_lex_capture_enabled", return_value=True),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value="LEX-LLC"),
            patch.object(fae, "_lex_sub_entity_allowed", return_value=True),
            patch.object(ma, "_is_phi_meeting", return_value=False),
            patch.object(fae, "_parse_action_items_with_haiku", return_value=parsed),
            patch.object(fae, "_scrub_lex_text", side_effect=lambda s: f"SCRUBBED({s})"),
        ):
            out = ma.run_meeting_action_items(ASKER, "LEX-LLC", _input(_channel_id="C_LLC", meeting_query="llc ops"))
        assert "SCRUBBED(" in out  # title + summary + items all scrubbed


# ---------------------------------------------------------------------------
# Confirm / create (staged write) — entry point
# ---------------------------------------------------------------------------

class TestConfirm:
    def test_confirm_requires_transcript_id(self, asker_identity):
        out = ma.run_meeting_action_items(
            ASKER, "F3E", _input(confirmed=True, selected_items=["x"])
        )
        assert "requires transcript_id" in out

    def test_confirm_requires_selected_items(self, asker_identity):
        with patch.object(ma, "_fetch_transcript_by_id", return_value=_mk_transcript()):
            out = ma.run_meeting_action_items(
                ASKER, "F3E", _input(confirmed=True, transcript_id="01TID")
            )
        assert "No items selected" in out

    def test_confirm_non_list_selected_items_coerced(self, asker_identity):
        # FIX (NIT): a malformed non-list/non-str selected_items is treated as no
        # selection (clean refusal, not a crash).
        with patch.object(ma, "_fetch_transcript_by_id", return_value=_mk_transcript()):
            out = ma.run_meeting_action_items(
                ASKER, "F3E", _input(confirmed=True, transcript_id="01TID", selected_items={"a": 1})
            )
        assert "No items selected" in out

    def test_confirm_non_attendee_refused(self, asker_identity):
        with (
            patch.object(ma, "_fetch_transcript_by_id", return_value=_mk_transcript()),
            patch.object(ma, "_asker_attended", return_value=False),
        ):
            out = ma.run_meeting_action_items(
                ASKER, "F3E",
                _input(confirmed=True, transcript_id="01TID", selected_items=["Send the deck"]),
            )
        assert "meetings you attended" in out

    def test_confirm_creates_tasks_assigned_to_asker(self, asker_identity):
        t = _mk_transcript(title="F3 Marketing Sync")
        created = {"gid": "T1", "permalink_url": "https://app.asana.com/t/1"}
        with (
            patch.object(ma, "_fetch_transcript_by_id", return_value=t),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="F3E"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value=None),
            patch.object(ma, "resolve_project", return_value="PROJ_F3E"),
            patch.object(ma, "is_blocked_project", return_value=False),
            patch.object(ma.asana_client, "find_recent_duplicate_task", return_value=None),
            patch.object(ma.asana_client, "set_task_custom_fields", return_value=True),
            patch.object(ma.asana_client, "create_task", return_value=created) as mock_create,
            patch.object(fae, "_capture_custom_fields", return_value={}),
        ):
            out = ma.run_meeting_action_items(
                ASKER, "F3E",
                _input(confirmed=True, transcript_id="01TID", selected_items=["Send the deck"]),
            )
        mock_create.assert_called_once()
        assert mock_create.call_args.kwargs["assignee_gid"] == "GID_ASKER"
        assert mock_create.call_args.kwargs["project_gid"] == "PROJ_F3E"
        assert "WRITE_CONFIRMED" in out and "Send the deck" in out

    def test_confirm_skips_fabricated_item(self, asker_identity):
        # FIX (MEDIUM): an item that doesn't match the meeting's action items is
        # not created (no fabricated/cross-meeting tasks on the write path).
        t = _mk_transcript(action_items="**Tommy Anderson**\nSend the proposal deck (Fri)\n")
        with (
            patch.object(ma, "_fetch_transcript_by_id", return_value=t),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="F3E"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value=None),
            patch.object(ma, "resolve_project", return_value="PROJ"),
            patch.object(ma, "is_blocked_project", return_value=False),
            patch.object(ma.asana_client, "create_task") as mock_create,
        ):
            out = ma.run_meeting_action_items(
                ASKER, "F3E",
                _input(confirmed=True, transcript_id="01TID",
                       selected_items=["buy a yacht in monaco"]),
            )
        mock_create.assert_not_called()
        assert "didn't match" in out

    def test_confirm_no_asana_gid_refused(self, asker_identity):
        t = _mk_transcript()
        with (
            patch.object(ma, "_fetch_transcript_by_id", return_value=t),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="F3E"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value=None),
            patch.object(ma, "_asker_asana_gid", return_value=None),
        ):
            out = ma.run_meeting_action_items(
                ASKER, "F3E",
                _input(confirmed=True, transcript_id="01TID", selected_items=["x"]),
            )
        assert "Asana mapping isn't set up" in out

    def test_confirm_skips_duplicate(self, asker_identity):
        t = _mk_transcript()  # action_items mentions "deck"
        with (
            patch.object(ma, "_fetch_transcript_by_id", return_value=t),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="F3E"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value=None),
            patch.object(ma, "resolve_project", return_value="PROJ"),
            patch.object(ma, "is_blocked_project", return_value=False),
            patch.object(ma.asana_client, "find_recent_duplicate_task", return_value="EXISTING"),
            patch.object(ma.asana_client, "create_task") as mock_create,
        ):
            out = ma.run_meeting_action_items(
                ASKER, "F3E",
                _input(confirmed=True, transcript_id="01TID", selected_items=["Send the deck"]),
            )
        mock_create.assert_not_called()
        assert "wasn't able to create" in out

    def test_confirm_lex_routes_to_lex_project_and_scrubs(self, asker_identity):
        t = _mk_transcript(title="LLC Ops Sync", action_items="**Tommy Anderson**\nFollow up re billing authorization (Fri)\n")
        created = {"gid": "T1", "permalink_url": "https://app.asana.com/t/1"}
        with (
            patch.object(ma, "_fetch_transcript_by_id", return_value=t),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="LEX"),
            patch.object(fae, "_lex_capture_enabled", return_value=True),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value="LEX-LLC"),
            patch.object(fae, "_lex_sub_entity_allowed", return_value=True),
            patch.object(ma, "_is_phi_meeting", return_value=False),
            patch.object(fae, "_resolve_lex_project", return_value="LEX_PROJ") as mock_lex_proj,
            patch.object(fae, "_capture_custom_fields", return_value={}),
            patch.object(fae, "_scrub_lex_text", side_effect=lambda s: f"SCRUB[{s}]"),
            patch.object(ma.asana_client, "find_recent_duplicate_task", return_value=None),
            patch.object(ma.asana_client, "set_task_custom_fields", return_value=True),
            patch.object(ma.asana_client, "create_task", return_value=created) as mock_create,
            patch.object(ma, "resolve_project") as mock_smart,
        ):
            out = ma.run_meeting_action_items(
                ASKER, "LEX-LLC",
                _input(_channel_id="C_LLC", confirmed=True, transcript_id="01TID",
                       selected_items=["Follow up re billing"]),
            )
        mock_smart.assert_not_called()              # LEX never uses the generic resolver
        mock_lex_proj.assert_called_once()          # LEX-scoped routing
        assert mock_create.call_args.kwargs["project_gid"] == "LEX_PROJ"
        assert mock_create.call_args.kwargs["name"].startswith("SCRUB[")  # scrubbed

    def test_confirm_lex_no_project_skips(self, asker_identity):
        # selected item matches the meeting (passes the FIX-4 content check) so it
        # reaches LEX project routing -- which returns None -> task is skipped,
        # NEVER created outside LEX scope.
        t = _mk_transcript(title="LLC Ops Sync", action_items="**Tommy Anderson**\nfollow up on billing authorization (Fri)\n")
        with (
            patch.object(ma, "_fetch_transcript_by_id", return_value=t),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="LEX"),
            patch.object(fae, "_lex_capture_enabled", return_value=True),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value="LEX-LLC"),
            patch.object(fae, "_lex_sub_entity_allowed", return_value=True),
            patch.object(ma, "_is_phi_meeting", return_value=False),
            patch.object(fae, "_resolve_lex_project", return_value=None) as mock_lex_proj,  # no LEX project
            patch.object(fae, "_scrub_lex_text", side_effect=lambda s: s),
            patch.object(ma.asana_client, "create_task") as mock_create,
        ):
            out = ma.run_meeting_action_items(
                ASKER, "LEX-LLC",
                _input(_channel_id="C_LLC", confirmed=True, transcript_id="01TID",
                       selected_items=["follow up on billing authorization"]),
            )
        mock_lex_proj.assert_called_once()  # reached LEX routing
        mock_create.assert_not_called()     # no LEX project -> never create outside scope
        assert "wasn't able to create" in out

    def test_confirm_lbhs_refused(self, asker_identity):
        t = _mk_transcript(title="Lex Sync")
        with (
            patch.object(ma, "_fetch_transcript_by_id", return_value=t),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="LEX"),
            patch.object(fae, "_lex_capture_enabled", return_value=True),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value="LEX-LBHS"),
            patch.object(fae, "_lex_sub_entity_allowed", return_value=False),
            patch.object(ma.asana_client, "create_task") as mock_create,
        ):
            out = ma.run_meeting_action_items(
                ASKER, "LEX-LLC",
                _input(_channel_id="C_LLC", confirmed=True, transcript_id="01TID",
                       selected_items=["x"]),
            )
        mock_create.assert_not_called()
        assert "confidentiality scope" in out

    def test_confirm_lex_scrub_does_not_drop_legit_item(self, asker_identity):
        # FIX (LOW, 2nd review): the content match must compare the scrubbed
        # selection against SCRUBBED action-items, else redaction drops a legit
        # LEX task. Here "Lucas" -> "client" via scrub; the selected (scrubbed)
        # item only matches the meeting after the action-items are scrubbed too.
        t = _mk_transcript(
            title="LLC Ops Sync",
            action_items="**Tommy Anderson**\nfollow up with Lucas about billing\n",
        )
        created = {"gid": "T1", "permalink_url": ""}
        with (
            patch.object(ma, "_fetch_transcript_by_id", return_value=t),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="LEX"),
            patch.object(fae, "_lex_capture_enabled", return_value=True),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value="LEX-LLC"),
            patch.object(fae, "_lex_sub_entity_allowed", return_value=True),
            patch.object(ma, "_is_phi_meeting", return_value=False),
            patch.object(fae, "_resolve_lex_project", return_value="LEX_PROJ"),
            patch.object(fae, "_capture_custom_fields", return_value={}),
            patch.object(fae, "_scrub_lex_text", side_effect=lambda s: s.replace("Lucas", "client")),
            patch.object(ma.asana_client, "find_recent_duplicate_task", return_value=None),
            patch.object(ma.asana_client, "set_task_custom_fields", return_value=True),
            patch.object(ma.asana_client, "create_task", return_value=created) as mock_create,
        ):
            out = ma.run_meeting_action_items(
                ASKER, "LEX-LLC",
                _input(_channel_id="C_LLC", confirmed=True, transcript_id="01TID",
                       selected_items=["follow up with client about billing"]),
            )
        mock_create.assert_called_once()  # not dropped by redaction
        assert "WRITE_CONFIRMED" in out

    def test_confirm_caps_selected_items(self, asker_identity):
        t = _mk_transcript(action_items="**Tommy Anderson**\ncomplete the report items list\n")
        created = {"gid": "T", "permalink_url": ""}
        many = [f"report item line {i}" for i in range(30)]  # each shares 'report'+'item'/'line'
        with (
            patch.object(ma, "_fetch_transcript_by_id", return_value=t),
            patch.object(ma, "_asker_attended", return_value=True),
            patch.object(ma, "_classify_entity", return_value="F3E"),
            patch.object(ma, "_tag_fireflies_sub_entity", return_value=None),
            patch.object(ma, "resolve_project", return_value="PROJ"),
            patch.object(ma, "is_blocked_project", return_value=False),
            patch.object(ma.asana_client, "find_recent_duplicate_task", return_value=None),
            patch.object(ma.asana_client, "set_task_custom_fields", return_value=True),
            patch.object(fae, "_capture_custom_fields", return_value={}),
            patch.object(ma.asana_client, "create_task", return_value=created) as mock_create,
        ):
            ma.run_meeting_action_items(
                ASKER, "F3E",
                _input(confirmed=True, transcript_id="01TID", selected_items=many),
            )
        assert mock_create.call_count == ma._MAX_SELECTED


# ---------------------------------------------------------------------------
# Recall / ingest path must be undisturbed by this additive build
# ---------------------------------------------------------------------------

class TestRecallUntouched:
    def test_extractor_run_action_capture_still_present(self):
        assert callable(getattr(fae, "run_action_capture", None))

    def test_fireflies_ingest_backfill_still_present(self):
        from cora.connectors import fireflies_connector as fc
        assert callable(getattr(fc, "backfill", None))
