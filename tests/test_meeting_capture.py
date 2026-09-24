"""One Cora Notetaker capture lane -- roster, ensure lane, daily auditor.

Build seed cq-ffcf6e4ffe7c, D-247 amendment. Every external read is injected, so
the whole diff and the whole planner are exercised without a network.

The cases that matter most here are not the happy paths. They are:
  * a meeting that exists as two calendar events (the ensure lane must act ONCE),
  * a LEX title never reaching the report or the ledger,
  * the write gates holding when only one of the two is open,
  * and a degraded read never rendering as a clean day.
"""

from __future__ import annotations

import json
import time
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import meeting_capture as mc  # noqa: E402

AZ = timezone(timedelta(hours=-7))
DAY = "2026-08-26"


def _ts(hh: int, mm: int = 0) -> str:
    """Timestamp on the audited day, tolerating hh >= 24 so a caller can express an
    end time that rolls past midnight (a 23:30 meeting ends at 00:30 the next day)."""
    return (
        datetime(2026, 8, 26, 0, mm, tzinfo=AZ) + timedelta(hours=hh)
    ).isoformat()


def _ev(
    eid: str,
    *,
    summary="Weekly Sync",
    hh=10,
    mm=0,
    link="https://meet.google.com/aaa-bbbb-ccc",
    organizer="harrison@hjrglobal.com",
    attendees=None,
    status="confirmed",
    event_type="default",
    location=None,
    description=None,
):
    ev = {
        "id": eid,
        "summary": summary,
        "status": status,
        "eventType": event_type,
        "start": {"dateTime": _ts(hh, mm)},
        "end": {"dateTime": _ts(hh + 1, mm)},
        "organizer": {"email": organizer},
        "attendees": [{"email": a} for a in (attendees or [organizer])],
    }
    if link:
        ev["hangoutLink"] = link
    if location:
        ev["location"] = location
    if description:
        ev["description"] = description
    return ev


def _cfg(**over) -> mc.CaptureConfig:
    base = dict(
        capture_identity="cora@hjrglobal.com",
        members=(
            mc.RosterMember("Harrison", "harrison@hjrglobal.com"),
            mc.RosterMember("Hannah", "hannah@hjrglobal.com"),
        ),
        skip_title_markers=("[no-bot]",),
        no_record_title_patterns=("counsel", "oneamerica"),
        no_record_emails=frozenset({"lawyer@outsidefirm.com"}),
        no_record_attendee_domains=("outsidefirm.com",),
    )
    base.update(over)
    return mc.CaptureConfig(**base)


def _t(tid, *, title="Weekly Sync", hh=10, cal_id=None, link="https://meet.google.com/aaa-bbbb-ccc",
       fred=None, organizer="harrison@hjrglobal.com"):
    return {
        "id": tid,
        "title": title,
        "date": int(datetime(2026, 8, 26, hh, 0, tzinfo=AZ).timestamp() * 1000),
        "cal_id": cal_id,
        "meeting_link": link,
        "organizer_email": organizer,
        "host_email": organizer,
        "meeting_info": {"fred_joined": fred},
        "meeting_attendees": [{"email": organizer, "displayName": "H"}],
    }


@pytest.fixture(autouse=True)
def _isolate_ledger(tmp_path, monkeypatch):
    """A new write path needs its redirect in the same commit as the writer."""
    monkeypatch.setenv("CORA_MEETING_CAPTURE_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.delenv("CORA_ONECORA_ENSURE", raising=False)
    mc._cfg_cache = None
    yield
    mc._cfg_cache = None


# ── flag ─────────────────────────────────────────────────────────────────────

class TestEnsureMode:
    def test_default_is_off(self):
        assert mc.ensure_mode() == "off"

    @pytest.mark.parametrize("val", ["plan", "live", "PLAN", " Live "])
    def test_recognised_values(self, monkeypatch, val):
        monkeypatch.setenv("CORA_ONECORA_ENSURE", val)
        assert mc.ensure_mode() == val.strip().lower()

    @pytest.mark.parametrize("val", ["1", "true", "on", "yes", "", "banana"])
    def test_unrecognised_values_fall_back_to_off(self, monkeypatch, val):
        """The CORA_AUTOWRITE_LIVE=1 trap: a truthy-looking value must not enable
        writes, and must not be silently accepted either."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", val)
        assert mc.ensure_mode() == "off"


# ── config ───────────────────────────────────────────────────────────────────

class TestConfig:
    def test_loads_the_real_roster(self):
        cfg = mc.load_config(force=True)
        assert cfg.capture_identity == "cora@hjrglobal.com"
        assert len(cfg.active_members) >= 5
        assert "[no-bot]" in cfg.skip_title_markers

    def test_real_roster_has_one_entry_per_physical_calendar(self):
        """D-096: tommy@hjrglobal and tommy@f3energy are the SAME calendar (verified
        live). Listing both would make the lane act on every event twice."""
        cfg = mc.load_config(force=True)
        locals_ = [m.calendar_email.split("@", 1)[0] for m in cfg.members]
        assert len(locals_) == len(set(locals_)), f"duplicate person in roster: {locals_}"

    def test_missing_file_raises_rather_than_serving_empty(self, tmp_path):
        with pytest.raises(mc.MeetingCaptureConfigError):
            mc.load_config(path=tmp_path / "nope.yaml")

    def test_malformed_file_raises(self, tmp_path):
        p = tmp_path / "r.yaml"
        p.write_text("just a string", encoding="utf-8")
        with pytest.raises(mc.MeetingCaptureConfigError):
            mc.load_config(path=p)

    def test_missing_capture_identity_raises(self, tmp_path):
        p = tmp_path / "r.yaml"
        p.write_text("roster: []\n", encoding="utf-8")
        with pytest.raises(mc.MeetingCaptureConfigError):
            mc.load_config(path=p)

    def test_duplicate_calendar_email_is_dropped(self, tmp_path):
        p = tmp_path / "r.yaml"
        p.write_text(
            'capture_identity: "cora@hjrglobal.com"\n'
            "roster:\n"
            '  - {name: "A", calendar_email: "a@hjrglobal.com"}\n'
            '  - {name: "A again", calendar_email: "A@hjrglobal.com"}\n',
            encoding="utf-8",
        )
        cfg = mc.load_config(path=p)
        assert len(cfg.members) == 1

    def test_disabled_member_excluded_but_retained(self, tmp_path):
        p = tmp_path / "r.yaml"
        p.write_text(
            'capture_identity: "cora@hjrglobal.com"\n'
            "roster:\n"
            '  - {name: "A", calendar_email: "a@hjrglobal.com", enabled: false}\n'
            '  - {name: "B", calendar_email: "b@hjrglobal.com"}\n',
            encoding="utf-8",
        )
        cfg = mc.load_config(path=p)
        assert len(cfg.members) == 2 and len(cfg.active_members) == 1


# ── qualification ────────────────────────────────────────────────────────────

class TestQualify:
    def test_plain_meeting_qualifies(self):
        assert mc.qualify_event(_ev("e1"), _cfg()).qualifies

    def test_cancelled_skipped(self):
        q = mc.qualify_event(_ev("e1", status="cancelled"), _cfg())
        assert not q.qualifies and q.reason == "cancelled"

    @pytest.mark.parametrize("etype", ["workingLocation", "outOfOffice", "focusTime", "fromGmail"])
    def test_non_meeting_event_types_skipped(self, etype):
        """'Office' and 'Dentist Appointment' render as these and were live on the
        roster on 2026-08-27."""
        q = mc.qualify_event(_ev("e1", event_type=etype), _cfg())
        assert not q.qualifies and q.reason.startswith("not-a-meeting")

    def test_no_link_skipped(self):
        q = mc.qualify_event(_ev("e1", link=None), _cfg())
        assert not q.qualifies and q.reason == "no-meeting-link"

    def test_declined_by_roster_user_skipped(self):
        ev = _ev("e1", attendees=["harrison@hjrglobal.com"])
        ev["attendees"] = [{"email": "harrison@hjrglobal.com", "responseStatus": "declined", "self": True}]
        q = mc.qualify_event(ev, _cfg(), roster_email="harrison@hjrglobal.com")
        assert not q.qualifies and q.reason == "roster-user-declined"

    def test_accepted_by_roster_user_still_qualifies(self):
        ev = _ev("e1")
        ev["attendees"] = [{"email": "harrison@hjrglobal.com", "responseStatus": "accepted", "self": True}]
        assert mc.qualify_event(ev, _cfg(), roster_email="harrison@hjrglobal.com").qualifies

    @pytest.mark.parametrize("title", ["[no-bot] Private chat", "Private [NO-BOT] chat"])
    def test_title_marker_skips(self, title):
        q = mc.qualify_event(_ev("e1", summary=title), _cfg())
        assert not q.qualifies and q.reason.startswith("title-marker")

    def test_no_record_title_pattern_skips(self):
        q = mc.qualify_event(_ev("e1", summary="Call with counsel"), _cfg())
        assert not q.qualifies and q.reason == "no-record-title:counsel"

    def test_no_record_pattern_is_word_bounded(self):
        """A carve-out that fires inside a longer word silently stops capture for a
        meeting that should be recorded -- the hardest failure to notice."""
        assert mc.qualify_event(_ev("e1", summary="Counselling program review"), _cfg()).qualifies
        assert mc.qualify_event(_ev("e2", summary="OneAmericas roadshow"), _cfg()).qualifies
        # but the real word still fires, in any case
        assert not mc.qualify_event(_ev("e3", summary="OneAmerica sync"), _cfg()).qualifies

    def test_no_record_email_skips_via_attendee(self):
        q = mc.qualify_event(
            _ev("e1", attendees=["harrison@hjrglobal.com", "lawyer@outsidefirm.com"]), _cfg()
        )
        assert not q.qualifies and q.reason.startswith("no-record-email")

    def test_no_record_domain_skips(self):
        q = mc.qualify_event(
            _ev("e1", attendees=["harrison@hjrglobal.com", "anyone@outsidefirm.com"]),
            _cfg(no_record_emails=frozenset()),
        )
        assert not q.qualifies and q.reason.startswith("no-record-domain")

    def test_empty_carve_out_lists_never_block(self):
        cfg = _cfg(skip_title_markers=(), no_record_title_patterns=(),
                   no_record_emails=frozenset(), no_record_attendee_domains=())
        assert mc.qualify_event(_ev("e1", summary="Call with counsel"), cfg).qualifies


# ── meeting identity ─────────────────────────────────────────────────────────

class TestMeetingKey:
    def test_two_event_ids_one_link_and_time_are_one_meeting(self):
        """Measured live 2026-08-26: an externally-organised call existed as
        `63b5da...` and `_f1jl4o...` on two roster calendars."""
        a = _ev("63b5da2780lt0hjbcpe6dcnarv", link="https://meet.google.com/kyj-dwct-uwg")
        b = _ev("_f1jl4obdal8m4hi16cr3io9kcho44", link="https://meet.google.com/kyj-dwct-uwg")
        assert mc.meeting_key(a) == mc.meeting_key(b)

    def test_same_link_different_time_is_a_different_meeting(self):
        a = _ev("a", hh=10)
        b = _ev("b", hh=14)
        assert mc.meeting_key(a) != mc.meeting_key(b)

    def test_link_case_is_normalised(self):
        a = _ev("a", link="https://MEET.google.com/AAA-bbbb-ccc")
        b = _ev("b", link="https://meet.google.com/aaa-bbbb-ccc")
        assert mc.meeting_key(a) == mc.meeting_key(b)

    def test_linkless_events_fall_back_to_event_id_and_never_collide(self):
        a = _ev("a", link=None)
        b = _ev("b", link=None)
        assert mc.meeting_key(a) != mc.meeting_key(b)


# ── LEX / PHI display rail ───────────────────────────────────────────────────

class TestDisplayTitle:
    def test_ordinary_title_passes_through(self):
        assert mc.display_title(_ev("e1", summary="F3 Weekly Meeting")) == "F3 Weekly Meeting"

    def test_lex_title_is_replaced_by_shape(self):
        ev = _ev("e1", summary="LLC client intake for Bob", organizer="shaun@lexingtonservices.com",
                 attendees=["shaun@lexingtonservices.com"])
        out = mc.display_title(ev)
        assert "Bob" not in out and "intake" not in out.lower()
        assert out.startswith("LEX/PHI meeting")

    def test_gov_client_attendee_redacts(self):
        """The live 2026-08-26 case: a .gov attendee marks a LEX client meeting."""
        ev = _ev("e1", summary="P. B. (In Person 90 Day @ 11AM)",
                 organizer="vreese@azdes.gov", attendees=["vreese@azdes.gov"])
        assert mc.display_title(ev).startswith("LEX/PHI meeting")

    def test_classifier_failure_redacts_rather_than_leaks(self, monkeypatch):
        import cora.connectors.fireflies_connector as ffc

        def boom(_):
            raise RuntimeError("classifier down")

        monkeypatch.setattr(ffc, "classify_lex_meeting", boom)
        assert mc.display_title(_ev("e1", summary="Secret client")).startswith("LEX/PHI")


# ── ensure lane ──────────────────────────────────────────────────────────────

def _lister(by_email: dict[str, list[dict]]):
    def _list(email: str, day: str):
        if email not in by_email:
            return []
        val = by_email[email]
        if isinstance(val, Exception):
            raise val
        return val
    return _list


class TestPlanEnsure:
    def test_acts_once_on_a_meeting_that_is_two_events(self):
        """Without this the lane puts N copies on the capture calendar and
        re-creates the duplicate-capture pattern it exists to remove."""
        link = "https://meet.google.com/kyj-dwct-uwg"
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [],
            "harrison@hjrglobal.com": [_ev("id-a", link=link, organizer="ext@vendor.com")],
            "hannah@hjrglobal.com": [_ev("_id-b", link=link, organizer="ext@vendor.com")],
        }))
        acting = [a for a in res.actions if a.action in ("guest-add", "copy")]
        assert len(acting) == 1
        assert "2 calendar copies" in acting[0].reason

    def test_prefers_the_copy_it_can_guest_add_to(self):
        link = "https://meet.google.com/kyj-dwct-uwg"
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [],
            "harrison@hjrglobal.com": [_ev("ext-copy", link=link, organizer="ext@vendor.com")],
            "hannah@hjrglobal.com": [_ev("own-copy", link=link, organizer="hannah@hjrglobal.com")],
        }))
        acting = [a for a in res.actions if a.action in ("guest-add", "copy")]
        assert len(acting) == 1
        assert acting[0].action == "guest-add" and acting[0].event_id == "own-copy"

    def test_in_domain_organizer_gets_guest_add(self):
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [],
            "harrison@hjrglobal.com": [_ev("e1", organizer="harrison@hjrglobal.com")],
        }))
        assert [a.action for a in res.actions if a.action != "skip"] == ["guest-add"]

    def test_external_organizer_gets_copy(self):
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [],
            "harrison@hjrglobal.com": [_ev("e1", organizer="someone@vendor.com")],
        }))
        assert [a.action for a in res.actions if a.action != "skip"] == ["copy"]

    def test_capture_identity_already_an_attendee_is_a_no_op(self):
        ev = _ev("e1", attendees=["harrison@hjrglobal.com", "cora@hjrglobal.com"])
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [],
            "harrison@hjrglobal.com": [ev],
        }))
        act = [a for a in res.actions if a.action != "skip"][0]
        assert act.action == "none" and act.reason == "already-covered"

    def test_link_already_on_the_capture_calendar_is_a_no_op(self):
        """Coverage is keyed on the LINK, so a meeting someone invited cora@ to by
        hand counts as covered -- which is what makes the lane safe to run
        alongside the manual habit during the overlap."""
        link = "https://meet.google.com/zzz-yyyy-xxx"
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [_ev("copy-on-cora", link=link)],
            "harrison@hjrglobal.com": [_ev("e1", link=link)],
        }))
        act = [a for a in res.actions if a.action != "skip"][0]
        assert act.action == "none"

    def test_unreadable_capture_calendar_plans_nothing(self):
        """Without knowing what is already covered we cannot tell covered from
        uncovered, so planning writes would risk duplicating every meeting."""
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": RuntimeError("403"),
            "harrison@hjrglobal.com": [_ev("e1")],
        }))
        assert res.actions == []
        assert res.failed_calendars and res.failed_calendars[0][0] == "cora@hjrglobal.com"

    def test_one_unreadable_member_calendar_is_named_not_swallowed(self):
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [],
            "harrison@hjrglobal.com": [_ev("e1")],
            "hannah@hjrglobal.com": RuntimeError("boom"),
        }))
        assert [e for e, _ in res.failed_calendars] == ["hannah@hjrglobal.com"]
        assert any(a.action == "guest-add" for a in res.actions)

    def test_carve_out_produces_a_skip_row_not_silence(self):
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [],
            "harrison@hjrglobal.com": [_ev("e1", summary="[no-bot] private")],
        }))
        assert [a.reason for a in res.actions] == ["title-marker:[no-bot]"]


class TestExecuteEnsure:
    def _plan(self):
        return mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [],
            "harrison@hjrglobal.com": [_ev("e1", organizer="harrison@hjrglobal.com")],
        }))

    def test_no_writes_when_flag_off_even_with_apply(self, monkeypatch):
        calls = []
        from cora.tools import calendar_client as cc
        monkeypatch.setattr(cc, "add_attendee", lambda **k: calls.append(k) or (True, "added"))
        res = mc.execute_ensure(self._plan(), _cfg(), apply=True)
        assert res.applied is False and calls == []

    def test_no_writes_in_plan_mode_with_apply(self, monkeypatch):
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "plan")
        calls = []
        from cora.tools import calendar_client as cc
        monkeypatch.setattr(cc, "add_attendee", lambda **k: calls.append(k) or (True, "added"))
        res = mc.execute_ensure(self._plan(), _cfg(), apply=True)
        assert res.applied is False and calls == []

    def test_no_writes_in_live_mode_without_apply(self, monkeypatch):
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        calls = []
        from cora.tools import calendar_client as cc
        monkeypatch.setattr(cc, "add_attendee", lambda **k: calls.append(k) or (True, "added"))
        res = mc.execute_ensure(self._plan(), _cfg(), apply=False)
        assert res.applied is False and calls == []

    def test_writes_only_when_both_gates_open(self, monkeypatch):
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        calls = []
        from cora.tools import calendar_client as cc
        monkeypatch.setattr(cc, "add_attendee", lambda **k: (calls.append(k), (True, "added"))[1])
        res = mc.execute_ensure(self._plan(), _cfg(), apply=True)
        assert res.applied is True
        assert len(calls) == 1
        assert calls[0]["attendee_email"] == "cora@hjrglobal.com"

    def test_guest_add_refusal_falls_back_to_copy(self, monkeypatch):
        """A 403 is the ordinary 'guests cannot invite others' case; the copy path
        exists precisely to cover what guest-add cannot reach."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        from cora.tools import calendar_client as cc

        def boom(**_k):
            raise cc.CalendarClientError("Calendar HTTP 403 forbiddenForNonOrganizer")

        copies = []
        monkeypatch.setattr(cc, "add_attendee", boom)
        monkeypatch.setattr(cc, "get_event", lambda **k: _ev("e1"))
        monkeypatch.setattr(cc, "insert_event_copy", lambda **k: copies.append(k) or {"id": "new"})
        res = mc.execute_ensure(self._plan(), _cfg(), apply=True)
        act = [a for a in res.actions if a.action == "copy"][0]
        assert act.applied is True and len(copies) == 1
        assert "guest-add refused" in act.reason

    def test_a_failure_is_recorded_not_raised(self, monkeypatch):
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        from cora.tools import calendar_client as cc

        def boom(**_k):
            raise cc.CalendarClientError("nope")

        monkeypatch.setattr(cc, "add_attendee", boom)
        monkeypatch.setattr(cc, "get_event", boom)
        monkeypatch.setattr(cc, "insert_event_copy", boom)
        res = mc.execute_ensure(self._plan(), _cfg(), apply=True)
        assert any(a.error for a in res.actions)


# ── auditor ──────────────────────────────────────────────────────────────────

def _audit(events: dict, transcripts: list, cfg=None, seats=None):
    return mc.audit_day(
        DAY, cfg or _cfg(),
        list_events=_lister(events),
        fetch_transcripts=lambda a, b: transcripts,
        fetch_seats=lambda: seats if seats is not None else [{"email": "harrison@hjrglobal.com"}],
    )


class TestAudit:
    def test_exact_join_on_cal_id(self):
        r = _audit(
            {"harrison@hjrglobal.com": [_ev("evt-1")]},
            [_t("t1", cal_id="evt-1")],
        )
        assert r.scheduled == 1 and r.captured == 1 and r.misses == []

    def test_falls_back_to_meeting_link_when_cal_id_absent(self):
        """About half of live transcripts carry no cal_id."""
        r = _audit(
            {"harrison@hjrglobal.com": [_ev("evt-1", link="https://meet.google.com/lnk-aaaa-bbb")]},
            [_t("t1", cal_id=None, link="https://meet.google.com/lnk-aaaa-bbb")],
        )
        assert r.captured == 1 and r.misses == []

    def test_falls_back_to_title(self):
        r = _audit(
            {"harrison@hjrglobal.com": [_ev("evt-1", summary="Board Sync")]},
            [_t("t1", title="board sync", cal_id=None, link="https://meet.google.com/other-xxx-yyy")],
        )
        assert r.captured == 1

    def test_uncaptured_meeting_is_a_miss(self):
        r = _audit({"harrison@hjrglobal.com": [_ev("evt-1")]}, [])
        assert r.scheduled == 1 and r.captured == 0 and len(r.misses) == 1

    def test_two_transcripts_for_one_meeting_is_a_duplicate(self):
        r = _audit(
            {"harrison@hjrglobal.com": [_ev("evt-1")]},
            [_t("t1", cal_id="evt-1"), _t("t2", cal_id="evt-1")],
        )
        assert len(r.duplicates) == 1 and r.captured == 1

    def test_transcript_matching_the_other_copy_of_a_meeting_still_joins(self):
        """cal_id may name whichever calendar copy Fireflies saw."""
        link = "https://meet.google.com/kyj-dwct-uwg"
        r = _audit(
            {
                "harrison@hjrglobal.com": [_ev("id-a", link=link, organizer="ext@v.com")],
                "hannah@hjrglobal.com": [_ev("_id-b", link=link, organizer="ext@v.com")],
            },
            [_t("t1", cal_id="_id-b", link=link)],
        )
        assert r.scheduled == 1 and r.captured == 1 and r.misses == []

    def test_transcript_with_no_matching_event_is_reported_unmatched(self):
        r = _audit(
            {"harrison@hjrglobal.com": [_ev("evt-1", summary="A", link="https://meet.google.com/a-a-a")]},
            [_t("t1", title="Somewhere else", cal_id=None, link="https://meet.google.com/z-z-z")],
        )
        assert len(r.unmatched_transcripts) == 1

    def test_transcript_outside_the_audited_day_is_ignored(self):
        far = _t("t1", cal_id="evt-1")
        far["date"] = int(datetime(2026, 8, 20, 10, tzinfo=AZ).timestamp() * 1000)
        r = _audit({"harrison@hjrglobal.com": [_ev("evt-1")]}, [far])
        assert r.captured == 0 and len(r.unmatched_transcripts) == 0

    def test_unreadable_calendar_is_named_and_flagged(self):
        r = _audit(
            {"harrison@hjrglobal.com": RuntimeError("403 denied"),
             "hannah@hjrglobal.com": [_ev("evt-1")]},
            [],
        )
        assert [e for e, _ in r.failed_calendars] == ["harrison@hjrglobal.com"]
        assert "1 calendar(s) unreadable" in mc.render_report(r)

    def test_fireflies_failure_never_renders_as_a_clean_day(self):
        r = mc.audit_day(
            DAY, _cfg(),
            list_events=_lister({"harrison@hjrglobal.com": [_ev("evt-1")]}),
            fetch_transcripts=lambda a, b: (_ for _ in ()).throw(RuntimeError("401")),
            fetch_seats=lambda: [],
        )
        out = mc.render_report(r)
        assert r.transcript_error
        assert "NOT trustworthy" in out
        assert "captured exactly once" not in out

    def test_seat_note_reports_capture_identity_not_yet_active(self):
        r = _audit({"harrison@hjrglobal.com": []}, [], seats=[{"email": "harrison@hjrglobal.com"}])
        assert "NOT YET ACTIVE" in r.seat_note

    def test_seat_note_reports_active_once_the_seat_exists(self):
        r = _audit({"harrison@hjrglobal.com": []}, [],
                   seats=[{"email": "cora@hjrglobal.com"}])
        assert "ACTIVE" in r.seat_note and "NOT YET" not in r.seat_note

    def test_structural_non_meetings_are_not_reported_as_carve_outs(self):
        r = _audit(
            {"harrison@hjrglobal.com": [
                _ev("e1", summary="Office", event_type="workingLocation"),
                _ev("e2", summary="Lunch", link=None),
            ]},
            [],
        )
        assert r.skipped == [] and r.scheduled == 0

    def test_real_carve_outs_are_reported(self):
        r = _audit({"harrison@hjrglobal.com": [_ev("e1", summary="[no-bot] private")]}, [])
        assert len(r.skipped) == 1 and r.skipped[0][1].startswith("title-marker")


class TestRenderReport:
    def test_clean_day_says_so(self):
        r = _audit({"harrison@hjrglobal.com": [_ev("evt-1")]}, [_t("t1", cal_id="evt-1")])
        assert "captured exactly once" in mc.render_report(r)

    def test_empty_day_does_not_claim_a_capture_success(self):
        """This report posts 7 days a week; a weekend has no meetings, and
        "captured exactly once" over a denominator of zero reads as a success it
        did not earn."""
        r = _audit({"harrison@hjrglobal.com": []}, [])
        out = mc.render_report(r)
        assert "No qualifying roster meetings scheduled" in out
        assert "captured exactly once" not in out

    def test_lex_title_never_appears_in_the_report(self):
        ev = _ev("e1", summary="Bob Smith intake assessment",
                 organizer="shaun@lexingtonservices.com", attendees=["shaun@lexingtonservices.com"])
        r = _audit({"harrison@hjrglobal.com": [ev]}, [])
        out = mc.render_report(r)
        assert "Bob Smith" not in out and "intake" not in out.lower()
        assert "LEX/PHI meeting" in out

    def test_angle_brackets_in_a_real_title_are_slack_escaped(self):
        """Live titles really do contain them -- "Harrison <> Lukas BevNET",
        "Tommy x Hannah <> Asana + HubSpot". In Slack mrkdwn `<...>` is link syntax,
        so an unescaped title renders mangled. sanitize_text deliberately preserves
        `<...>` for real link markup, so the escaping must happen per-fragment."""
        r = _audit({"harrison@hjrglobal.com": [_ev("e1", summary="Harrison <> Lukas & Co")]}, [])
        out = mc.render_report(r)
        assert "&lt;&gt;" in out and "&amp;" in out
        assert "<> Lukas" not in out

    def test_escaping_does_not_touch_our_own_markup(self):
        r = _audit({"harrison@hjrglobal.com": [_ev("e1", summary="Plain Title")]}, [])
        out = mc.render_report(r)
        assert "*:red_circle:" in out and "_(organizer" in out

    def test_misses_are_listed_chronologically(self):
        r = _audit(
            {"harrison@hjrglobal.com": [
                _ev("late", summary="Late", hh=16, link="https://meet.google.com/l-l-l"),
                _ev("early", summary="Early", hh=8, link="https://meet.google.com/e-e-e"),
            ]},
            [],
        )
        out = mc.render_report(r)
        assert out.index("Early") < out.index("Late")


# ── ledger ───────────────────────────────────────────────────────────────────

class TestLedger:
    def test_append_only_round_trip(self, tmp_path):
        mc.write_ledger([{"a": 1}])
        mc.write_ledger([{"a": 2}])
        rows = [json.loads(l) for l in mc.ledger_path().read_text(encoding="utf-8").splitlines()]
        assert [r["a"] for r in rows] == [1, 2]

    def test_empty_write_is_a_no_op(self):
        mc.write_ledger([])
        assert not mc.ledger_path().exists()

    def test_write_failure_never_raises(self, monkeypatch, tmp_path):
        """A ledger failure must not take down the lane it is recording."""
        monkeypatch.setenv("CORA_MEETING_CAPTURE_LEDGER", str(tmp_path / "nodir" / "x" / "l.jsonl"))
        monkeypatch.setattr(Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
        mc.write_ledger([{"a": 1}])   # must not raise


# ── meeting-link extraction (calendar_client) ────────────────────────────────

class TestMeetingLinkExtraction:
    def test_video_entry_point_wins_over_a_leading_phone_entry(self):
        from cora.tools.calendar_client import extract_meeting_link

        ev = {"conferenceData": {"entryPoints": [
            {"entryPointType": "phone", "uri": "tel:+15551234"},
            {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"},
        ]}}
        assert extract_meeting_link(ev) == "https://meet.google.com/abc-defg-hij"

    def test_hangout_link_fallback(self):
        from cora.tools.calendar_client import extract_meeting_link

        assert extract_meeting_link({"hangoutLink": "https://meet.google.com/x"}) == \
            "https://meet.google.com/x"

    def test_allowlisted_host_in_location(self):
        from cora.tools.calendar_client import extract_meeting_link

        assert extract_meeting_link({"location": "Room B, https://f3.zoom.us/j/123"}) == \
            "https://f3.zoom.us/j/123"

    def test_non_meeting_url_is_not_a_meeting_link(self):
        from cora.tools.calendar_client import extract_meeting_link

        assert extract_meeting_link({"location": "https://docs.google.com/agenda"}) == ""

    def test_no_link_returns_empty(self):
        from cora.tools.calendar_client import extract_meeting_link

        assert extract_meeting_link({}) == ""


# ── scripts + deployment ─────────────────────────────────────────────────────

class TestScriptsAndDeployment:
    def test_setup_ps1_is_ascii_only(self):
        """D-016. There is NO repo-wide ASCII guard over deployment/*.ps1 -- only two
        files are covered elsewhere -- so a stray em-dash here would pass the full
        suite green and break at runtime under PowerShell 5.1."""
        p = _REPO_ROOT / "deployment" / "setup-meeting-capture-audit-task.ps1"
        raw = p.read_bytes()
        bad = [(i, b) for i, b in enumerate(raw) if b > 127]
        assert not bad, f"non-ASCII bytes at offsets {[i for i, _ in bad[:5]]}"

    def test_setup_ps1_uses_venv_python_not_uv(self):
        """D-005."""
        text = (_REPO_ROOT / "deployment" / "setup-meeting-capture-audit-task.ps1").read_text(
            encoding="ascii"
        )
        assert r".venv\Scripts\python.exe" in text
        assert "uv run" not in text

    def test_audit_script_is_structurally_read_only(self):
        """The auditor must never acquire a calendar-write or KB-write path. If a
        future edit reaches for one, this fails before it reaches production."""
        text = (_REPO_ROOT / "scripts" / "run_meeting_capture_audit.py").read_text(encoding="utf-8")
        for forbidden in ("insert_event_copy", "add_attendee", "create_event",
                          "delete_event", "upsert_documents", "set_own_response"):
            assert forbidden not in text, f"auditor must not reference {forbidden}"

    def test_audit_ledger_records_ids_never_titles(self):
        """A LEX title must not reach an at-rest log any more than the ops channel."""
        text = (_REPO_ROOT / "scripts" / "run_meeting_capture_audit.py").read_text(encoding="utf-8")
        assert "missed_event_ids" in text
        assert "m.title" not in text and "\"title\"" not in text

    def test_ensure_script_does_nothing_at_all_when_the_flag_is_off(self, monkeypatch, capsys):
        """Behavioural, not textual. An earlier version of this test asserted that
        `ensure_mode()` appeared before `plan_ensure` in the SOURCE, which is both
        false (the helper is defined above main) and meaningless -- source order is
        not execution order. What matters is that with the flag off the script reads
        nobody's calendar and returns cleanly.
        """
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_ensure_script", _REPO_ROOT / "scripts" / "run_meeting_capture_ensure.py"
        )
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setattr(sys, "argv", ["run_meeting_capture_ensure.py", "--apply"])
        spec.loader.exec_module(mod)

        # ORDER IS LOAD-BEARING: clear the flag AFTER exec_module, never before.
        # The script does `load_dotenv(_REPO_ROOT / ".env", override=True)` at
        # module scope, so executing it re-populates anything set in .env -- and
        # .env now carries CORA_ONECORA_ENSURE=live (the lane was turned on after
        # this test was written). Clearing first therefore had no effect: the
        # lane ran, the monkeypatched plan_ensure returned None, and the test
        # died with an AttributeError deep inside meeting_capture rather than
        # testing anything. Reordering this back would silently re-break it.
        monkeypatch.delenv("CORA_ONECORA_ENSURE", raising=False)
        assert mc.ensure_mode() == "off", (
            "precondition: the flag must be unset for this test; something "
            "re-populated CORA_ONECORA_ENSURE after exec_module"
        )

        called: list[str] = []
        monkeypatch.setattr(mc, "plan_ensure", lambda *a, **k: called.append("plan"))
        monkeypatch.setattr(mc, "load_config", lambda *a, **k: called.append("cfg"))
        assert mod.main() == 0
        assert called == [], "the ensure lane must not even load config when off"

    def test_ensure_ledger_drops_structural_noise_but_keeps_decisions(self, monkeypatch):
        """At a 15-minute cadence a row per action per run is ~2,100 rows/day and
        ~154 MB/year -- half the logs+ledgers alarm threshold -- almost all of it
        "Office" and "Lunch" re-recorded 96 times a day."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_ensure_script2", _REPO_ROOT / "scripts" / "run_meeting_capture_ensure.py"
        )
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setattr(sys, "argv", ["run_meeting_capture_ensure.py"])
        spec.loader.exec_module(mod)

        def act(**kw):
            base = dict(member="M", calendar_email="m@hjrglobal.com", event_id="e",
                        title="t", start_label="10:00", action="skip", reason="")
            base.update(kw)
            return mc.EnsureAction(**base)

        # dropped: re-derived every run, never interesting afterwards
        assert not mod._ledger_worthy(act(reason="not-a-meeting:workinglocation"))
        assert not mod._ledger_worthy(act(reason="no-meeting-link"))
        assert not mod._ledger_worthy(act(reason="cancelled"))
        assert not mod._ledger_worthy(act(action="none", reason="already-covered"))
        # kept: consent decisions, planned work, real writes, failures
        assert mod._ledger_worthy(act(reason="title-marker:[no-bot]"))
        assert mod._ledger_worthy(act(reason="no-record-title:counsel"))
        assert mod._ledger_worthy(act(action="guest-add", reason="x"))
        assert mod._ledger_worthy(act(action="none", reason="already-covered", applied=True))
        assert mod._ledger_worthy(act(action="copy", reason="x", error="boom"))
        # RSVP sub-step (cq-19b0298cf5be): kept = a real accept, a failure, the
        # one-mechanism rule firing after a guest-add; dropped = re-derived noise
        assert mod._ledger_worthy(act(action="none", reason="already-covered", rsvp="accepted"))
        assert mod._ledger_worthy(act(action="none", reason="already-covered", rsvp="error"))
        assert mod._ledger_worthy(act(action="none", reason="already-covered",
                                      rsvp="skipped:notetaker-present"))
        assert not mod._ledger_worthy(act(action="none", reason="already-covered",
                                          rsvp="already-accepted"))
        # DELIBERATE FLIP (Code #14 R14-4, ruling 9.3): the lex-withheld outcome is
        # retired; a LEX accept is a real write and is kept like any accept.
        assert mod._ledger_worthy(act(action="none", reason="already-covered",
                                      rsvp="accepted:lex"))
        assert not mod._ledger_worthy(act(
            action="skip", reason=f"{mc.RSVP_NO_ROSTER_COPY_REASON}: no roster copy visible",
            rsvp="skipped:no-roster-copy"))

    def test_slack_post_goes_through_the_egress_boundary(self):
        """B1 doctrine. The CI guard is only an either/or tripwire, so pin the
        explicit sanitize call here."""
        text = (_REPO_ROOT / "scripts" / "run_meeting_capture_audit.py").read_text(encoding="utf-8")
        assert "sanitize_text" in text and "normalize_slack_bold" in text
        assert "mc.OPS_CHANNEL" in text

    def test_ops_channel_is_pinned_by_id(self):
        assert mc.OPS_CHANNEL == "C0BCUBUDHAR"


# ── D-051 remediation regressions ────────────────────────────────────────────
# One test per confirmed defect. Each names the failure it prevents, because a
# regression test whose purpose is not written down gets deleted by the next
# person who finds it inconvenient.

class TestReviewRemediations:
    def test_carve_out_on_one_copy_vetoes_the_whole_meeting(self):
        """CONSENT. A [no-bot] Harrison types on HIS copy must not be defeated by
        Hannah's copy of the same meeting still carrying the original title."""
        link = "https://meet.google.com/vet-oooo-aaa"
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [],
            "harrison@hjrglobal.com": [_ev("mine", summary="[no-bot] private", link=link)],
            "hannah@hjrglobal.com": [_ev("theirs", summary="Weekly Sync", link=link)],
        }))
        assert [a.action for a in res.actions] == ["skip"]
        assert res.actions[0].reason.startswith("title-marker")

    def test_second_meeting_on_a_shared_room_link_is_still_ensured(self):
        """A static personal room link is reused all day. Keying coverage on the
        link alone marked the second meeting already-covered -- a silent miss."""
        link = "https://meet.google.com/static-room-xyz"
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [_ev("cov", link=link, hh=9)],
            "harrison@hjrglobal.com": [_ev("later", link=link, hh=15)],
        }))
        acting = [a for a in res.actions if a.action in ("guest-add", "copy")]
        assert len(acting) == 1 and acting[0].event_id == "later"

    def test_legacy_notetaker_invite_blocks_a_second_bot(self):
        """The one-mechanism rule. Adding cora@ on top of an already-invited
        notetaker dispatches TWO bots -- the exact duplicate this build removes."""
        ev = _ev("e1", attendees=["harrison@hjrglobal.com", mc.LEGACY_NOTETAKER])
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [], "harrison@hjrglobal.com": [ev],
        }))
        act = [a for a in res.actions if a.action != "skip"][0]
        assert act.action == "none" and "legacy notetaker" in act.reason

    def test_carve_out_titles_are_never_published(self):
        """A no-record meeting is one somebody ruled must not be recorded; printing
        its title into a shared ops channel publishes what the rule protects."""
        r = _audit({"harrison@hjrglobal.com": [
            _ev("e1", summary="Strategy call with counsel re: Acme dispute")]}, [])
        out = mc.render_report(r)
        assert "Acme" not in out
        assert "Strategy call" not in out
        assert "a meeting at" in out

    def test_redacted_meeting_does_not_name_its_organizer(self):
        """An agency address beside a redacted title re-identifies the client
        programme that the redaction existed to protect."""
        ev = _ev("e1", summary="P. B. 90 day", organizer="vreese@azdes.gov",
                 attendees=["vreese@azdes.gov"])
        r = _audit({"harrison@hjrglobal.com": [ev]}, [])
        out = mc.render_report(r)
        assert "azdes.gov" not in out and "P. B." not in out
        assert "withheld" in out

    def test_transcript_rail_applies_the_client_domain_screen_too(self):
        """It was on the event rail only, leaving the transcript rail -- which
        renders captures with NO calendar event -- the weaker of the two."""
        t = _t("t1", title="P. B. 90 day review", organizer="vreese@azdes.gov")
        t["meeting_attendees"] = [{"email": "vreese@azdes.gov", "displayName": "V"}]
        assert mc._transcript_display_title(t).startswith("LEX/PHI meeting")

    def test_two_meetings_sharing_a_link_do_not_cross_match(self):
        """setdefault bound a shared link to whichever meeting was seen first --
        a false duplicate on one row and a false miss on another."""
        link = "https://meet.google.com/shared-room"
        r = _audit(
            {"harrison@hjrglobal.com": [
                _ev("m1", summary="Morning", hh=9, link=link),
                _ev("m2", summary="Afternoon", hh=15, link=link),
            ]},
            [_t("t1", title="Something else", hh=9, cal_id=None, link=link)],
        )
        assert r.duplicates == []
        assert len(r.unmatched_transcripts) == 1

    def test_two_meetings_sharing_a_title_do_not_cross_match(self):
        r = _audit(
            {"harrison@hjrglobal.com": [
                _ev("m1", summary="Standup", hh=9, link="https://meet.google.com/a-a-a"),
                _ev("m2", summary="Standup", hh=15, link="https://meet.google.com/b-b-b"),
            ]},
            [_t("t1", title="Standup", hh=9, cal_id=None, link="https://meet.google.com/z-z-z")],
        )
        assert r.duplicates == []
        assert len(r.unmatched_transcripts) == 1

    def test_add_attendee_refuses_a_truncated_guest_list(self):
        """Google sets attendeesOmitted when the list it returned is INCOMPLETE.
        Patching it back would delete every guest it left out."""
        import unittest.mock as um

        from cora.tools import calendar_client as cc

        with um.patch.object(cc, "get_event", return_value={
            "id": "e1", "attendeesOmitted": True,
            "attendees": [{"email": "one@hjrglobal.com"}],
        }):
            with pytest.raises(cc.CalendarClientError, match="attendeesOmitted"):
                cc.add_attendee(user_email="h@hjrglobal.com", event_id="e1",
                                attendee_email="cora@hjrglobal.com")

    def test_carve_out_survives_odd_whitespace(self):
        cfg = _cfg(no_record_title_patterns=("estate planning",))
        for title in ("Estate  Planning sync", "Estate Planning sync", "Estate\nPlanning"):
            assert not mc.qualify_event(_ev("e1", summary=title), cfg).qualifies
        assert mc.qualify_event(_ev("e2", summary="Real estate plans"), cfg).qualifies

    def test_malformed_carve_out_block_refuses_to_run(self, tmp_path):
        """Treating a broken carve_outs block as empty would capture every meeting
        the list was written to protect."""
        f = tmp_path / "r.yaml"
        f.write_text(
            'capture_identity: "cora@hjrglobal.com"\n'
            "roster:\n  - {name: A, calendar_email: a@hjrglobal.com}\n"
            "carve_outs: not-a-mapping\n", encoding="utf-8")
        with pytest.raises(mc.MeetingCaptureConfigError):
            mc.load_config(path=f)

    def test_scalar_where_a_carve_out_list_belongs_refuses_to_run(self, tmp_path):
        """A bare string is valid YAML; iterating it yields CHARACTERS, so the
        carve-out silently protects nobody."""
        f = tmp_path / "r.yaml"
        f.write_text(
            'capture_identity: "cora@hjrglobal.com"\n'
            "roster:\n  - {name: A, calendar_email: a@hjrglobal.com}\n"
            "carve_outs:\n  no_record_title_patterns: counsel\n", encoding="utf-8")
        with pytest.raises(mc.MeetingCaptureConfigError, match="must be a list"):
            mc.load_config(path=f)

    def test_quoted_false_actually_disables_a_roster_member(self, tmp_path):
        """bool of the string "false" is True -- a quoted value would keep someone
        in the sweep after Harrison had switched them off."""
        f = tmp_path / "r.yaml"
        f.write_text(
            'capture_identity: "cora@hjrglobal.com"\n'
            'roster:\n  - {name: A, calendar_email: a@hjrglobal.com, enabled: "false"}\n',
            encoding="utf-8")
        cfg = mc.load_config(path=f)
        assert cfg.active_members == ()

    def test_degraded_fireflies_join_is_announced(self, monkeypatch):
        """Without cal_id the whole diff runs on fallbacks; that must not be silent."""
        from cora.connectors import fireflies_connector as ffc

        monkeypatch.setattr(ffc, "_extended_query_unavailable", True)
        r = _audit({"harrison@hjrglobal.com": [_ev("e1")]}, [])
        assert "exact" in r.transcript_error and "cal_id" in r.transcript_error
        assert "NOT trustworthy" in mc.render_report(r)

    def test_action_extractor_shares_the_query_fallback(self):
        """Protecting only the NEW caller of a shared function leaves the sibling
        lane to go dark on the same vendor change."""
        text = (_REPO_ROOT / "src" / "cora" / "connectors"
                / "fireflies_action_extractor.py").read_text(encoding="utf-8")
        assert "_query_transcripts(variables)" in text
        assert "_graphql_query(_TRANSCRIPTS_QUERY" not in text


class TestReviewRemediationsRoundTwo:
    def test_all_day_event_is_not_a_capturable_meeting(self):
        """No start time for a notetaker to join at, and copying it puts an all-day
        event on the capture calendar that no bot can act on."""
        ev = _ev("e1")
        ev["start"] = {"date": "2026-08-26"}
        ev["end"] = {"date": "2026-08-27"}
        q = mc.qualify_event(ev, _cfg())
        assert not q.qualifies and q.reason == "all-day"

    def test_cross_midnight_meeting_belongs_to_the_day_it_starts(self):
        """events.list returns everything OVERLAPPING the window, so a 23:30-00:30
        meeting comes back on BOTH days; counting it twice makes the second day a
        permanent false miss."""
        late = _ev("late", hh=23, mm=30)
        assert mc.starts_on_day(late, "2026-08-26") is True
        assert mc.starts_on_day(late, "2026-08-27") is False

    def test_audit_does_not_double_count_a_cross_midnight_meeting(self):
        late = _ev("late", hh=23, mm=30)
        r_start = _audit({"harrison@hjrglobal.com": [late]}, [])
        assert r_start.scheduled == 1
        r_next = mc.audit_day(
            "2026-08-27", _cfg(),
            list_events=_lister({"harrison@hjrglobal.com": [late]}),
            fetch_transcripts=lambda a, b: [],
            fetch_seats=lambda: [],
        )
        assert r_next.scheduled == 0

    def test_stale_capture_copy_is_detected(self):
        """A copy is a snapshot. When the source moves or is cancelled the copy
        stays behind and sends the notetaker to that link at a time nobody agreed
        to -- a capture nobody consented to."""
        from cora.tools.calendar_client import CAPTURE_COPY_MARKER

        ghost = _ev("ghost", link="https://meet.google.com/gone-aaaa-bbb", hh=9)
        ghost["description"] = f"Capture copy ... ({CAPTURE_COPY_MARKER})\nsource_event_id: x"
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [ghost],
            "harrison@hjrglobal.com": [],      # source is gone
        }))
        assert [e for e, _ in res.stale_copies] == ["ghost"]

    def test_a_live_capture_copy_is_not_treated_as_stale(self):
        from cora.tools.calendar_client import CAPTURE_COPY_MARKER

        link = "https://meet.google.com/still-here-xx"
        copy = _ev("copy", link=link, hh=9)
        copy["description"] = f"({CAPTURE_COPY_MARKER})"
        src = _ev("src", link=link, hh=9, organizer="ext@vendor.com")
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [copy],
            "harrison@hjrglobal.com": [src],
        }))
        assert res.stale_copies == []

    def test_a_human_event_on_the_capture_calendar_is_never_touched(self):
        """Only events carrying this lane's own marker are ever deletion
        candidates."""
        human = _ev("human-made", link="https://meet.google.com/human-xxx", hh=9)
        human["description"] = "Harrison put this here on purpose"
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [human],
            "harrison@hjrglobal.com": [],
        }))
        assert res.stale_copies == []

    def test_stale_copies_are_only_deleted_under_both_write_gates(self, monkeypatch):
        from cora.tools import calendar_client as cc
        from cora.tools.calendar_client import CAPTURE_COPY_MARKER

        ghost = _ev("ghost", link="https://meet.google.com/gone-x", hh=9)
        ghost["description"] = f"({CAPTURE_COPY_MARKER})"
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [ghost], "harrison@hjrglobal.com": [],
        }))
        deleted: list = []
        monkeypatch.setattr(cc, "delete_event", lambda **k: deleted.append(k))

        mc.execute_ensure(res, _cfg(), apply=True)          # flag off
        assert deleted == []
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        mc.execute_ensure(res, _cfg(), apply=False)         # no --apply
        assert deleted == []
        mc.execute_ensure(res, _cfg(), apply=True)          # both gates
        assert [d["event_id"] for d in deleted] == ["ghost"]


class TestCarveOutBreachAlarm:
    """A carve-out that was recorded anyway is the most serious thing this auditor
    can find. Dropping carved meetings from the diff entirely would hide it."""

    def _carved_and_captured(self):
        ev = _ev("e1", summary="[no-bot] private chat",
                 link="https://meet.google.com/carve-out-x")
        t = _t("t1", title="private chat", cal_id="e1",
               link="https://meet.google.com/carve-out-x")
        return _audit({"harrison@hjrglobal.com": [ev]}, [t])

    def test_a_carved_out_meeting_that_was_recorded_is_alarmed(self):
        r = self._carved_and_captured()
        assert len(r.carve_out_breaches) == 1
        out = mc.render_report(r)
        assert "RECORDED DESPITE A CARVE-OUT" in out

    def test_the_breach_line_shows_shape_not_title(self):
        r = self._carved_and_captured()
        out = mc.render_report(r)
        assert "private chat" not in out
        assert "a meeting at" in out

    def test_a_breach_is_not_counted_as_an_unmatched_transcript(self):
        """It is not an unexplained capture -- we know exactly which meeting it is."""
        r = self._carved_and_captured()
        assert r.unmatched_transcripts == []

    def test_a_carved_out_meeting_is_not_scheduled_and_not_a_miss(self):
        """Reporting it as a miss would nag Harrison to close a gap he created."""
        r = _audit({"harrison@hjrglobal.com": [
            _ev("e1", summary="[no-bot] private chat")]}, [])
        assert r.scheduled == 0 and r.misses == [] and len(r.skipped) == 1

    def test_a_breach_suppresses_the_clean_day_line(self):
        r = self._carved_and_captured()
        assert "captured exactly once" not in mc.render_report(r)

    def test_audit_applies_the_cross_copy_veto_like_the_ensure_lane(self):
        """A [no-bot] on one copy must veto the meeting on every calendar, or the
        auditor reports a deliberately-excluded meeting as a miss."""
        link = "https://meet.google.com/two-copies-x"
        r = _audit({
            "harrison@hjrglobal.com": [_ev("mine", summary="[no-bot] private", link=link)],
            "hannah@hjrglobal.com": [_ev("theirs", summary="Weekly Sync", link=link)],
        }, [])
        assert r.scheduled == 0 and r.misses == []
        assert len(r.skipped) == 1

    def test_ensure_reason_withholds_the_organizer_of_a_redacted_meeting(self):
        """The reason is printed and persisted, so naming an agency address there
        would undo the redaction two fields away."""
        ev = _ev("e1", summary="P. B. 90 day", organizer="vreese@azdes.gov",
                 attendees=["vreese@azdes.gov"])
        res = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            "cora@hjrglobal.com": [], "harrison@hjrglobal.com": [ev],
        }))
        act = [a for a in res.actions if a.action in ("guest-add", "copy")][0]
        assert "azdes.gov" not in act.reason and "withheld" in act.reason
        assert act.title.startswith("LEX/PHI")

    def test_audit_docstring_time_matches_the_registered_task(self):
        doc = (_REPO_ROOT / "scripts" / "run_meeting_capture_audit.py").read_text(
            encoding="utf-8")
        ps1 = (_REPO_ROOT / "deployment" / "setup-meeting-capture-audit-task.ps1").read_text(
            encoding="ascii")
        import re as _re
        hh = _re.search(r'\$HourMin\s*=\s*"(\d\d:\d\d)"', ps1).group(1)
        assert hh in doc, f"docstring does not mention the registered time {hh}"


# ── presumed-unconvened bucket (Code #13 slice 4, cq-8c4f2f4e73fc) ───────────
# The 2026-09-07 (Labor Day) audit posted "5 scheduled, 0 captured, 5 missed" --
# and every one of the five was a recurring block organised by a Google GROUP
# calendar that nobody joined. There is no Meet/Zoom audit-log read available
# (no admin.reports scope, no Zoom API), so the classifier is structural and the
# bucket is labelled a PRESUMPTION everywhere it surfaces. Fixture text is
# synthetic (D-145/D-256): random-word titles, no real people, no client content.

_GROUP_CAL = "c_quartz7lantern@group.calendar.google.com"


def _labor_day_events() -> list[dict]:
    """The frozen 9/7 shape: five group-calendar recurring instances at 09:30,
    09:30, 10:00, 10:00, 17:00 with distinct links; two carry the legacy notetaker
    bot as an attendee (a bot was dispatched and still recorded nothing)."""
    spec = [
        ("gc-1_20260826T163000Z", "Velvet Otter Cadence", 9, 30, "https://meet.google.com/vot-cade-nce", True),
        ("gc-2_20260826T163000Z", "Brass Kite Ledger", 9, 30, "https://meet.google.com/bra-kite-ldg", False),
        ("gc-3_20260826T170000Z", "Marble Fern Relay", 10, 0, "https://meet.google.com/mar-fern-rly", True),
        ("gc-4_20260826T170000Z", "Copper Lantern Drift", 10, 0, "https://meet.google.com/cop-lant-drf", False),
        ("gc-5_20260827T000000Z", "Pewter Comet Ledger", 17, 0, "https://meet.google.com/pew-come-ldg", False),
    ]
    out = []
    for eid, title, hh, mm, link, with_bot in spec:
        attendees = ["harrison@hjrglobal.com", "pal@example.test"]
        if with_bot:
            attendees.append(mc.LEGACY_NOTETAKER)
        ev = _ev(eid, summary=title, hh=hh, mm=mm, link=link,
                 organizer=_GROUP_CAL, attendees=attendees)
        ev["recurringEventId"] = eid.split("_", 1)[0]
        out.append(ev)
    return out


class TestPresumedUnconvened:
    def test_labor_day_shape_is_unconvened_not_missed(self):
        """The 9/7 audit accused the capture lane of five misses that were five
        standing group-calendar blocks nobody joined. Prevents: a holiday of
        placeholders rendering as a capture-lane failure."""
        r = _audit({"harrison@hjrglobal.com": _labor_day_events()}, [])
        assert r.scheduled == 5
        assert r.captured == 0
        assert r.misses == []
        assert len(r.unconvened) == 5

    def test_c2_group_calendar_meeting_a_roster_human_accepted_is_a_miss(self):
        """D-051 review C-2 (the review's exact input): organiser is a group
        calendar, two roster humans RSVP'd `accepted`, a link is present, the
        bot failed to join -> zero transcripts. That is a capture failure and must
        be a MISS above the alarms -- never 'presumed unconvened' below them with
        the clean line printing. The signal is read on ANY copy of the meeting;
        an external or a bot accepting is not join evidence."""
        ev = _ev("gc-acc", summary="Velvet Otter Cadence", organizer=_GROUP_CAL,
                 link="https://meet.google.com/vot-cade-nce",
                 attendees=["harrison@hjrglobal.com", "hannah@hjrglobal.com", "pal@example.test"])
        for att in ev["attendees"]:
            if att["email"].endswith("@hjrglobal.com"):
                att["responseStatus"] = "accepted"
        r = _audit({"harrison@hjrglobal.com": [ev]}, [])
        assert r.unconvened == []
        assert len(r.misses) == 1 and r.misses[0].unconvened_basis == ""
        out = mc.render_report(r)
        assert "*:red_circle: Not captured (1)*" in out
        assert ":white_check_mark:" not in out
        assert "1 scheduled, 0 captured, 1 missed, 0 presumed unconvened" in out
        # ANY copy: Harrison's copy carries no RSVP, Hannah's copy says accepted.
        plain = _ev("gc-acc-h", summary="Velvet Otter Cadence", organizer=_GROUP_CAL,
                    link="https://meet.google.com/vot-cade-nce",
                    attendees=["harrison@hjrglobal.com", "hannah@hjrglobal.com"])
        hers = _ev("gc-acc-n", summary="Velvet Otter Cadence", organizer=_GROUP_CAL,
                   link="https://meet.google.com/vot-cade-nce",
                   attendees=["harrison@hjrglobal.com", "hannah@hjrglobal.com"])
        hers["attendees"][1]["responseStatus"] = "accepted"
        r2 = _audit({"harrison@hjrglobal.com": [plain], "hannah@hjrglobal.com": [hers]}, [])
        assert r2.scheduled == 1 and len(r2.misses) == 1 and r2.unconvened == []
        # A non-roster acceptance (external, or the bot) leaves the presumption.
        ext = _ev("gc-ext", summary="Brass Kite Ledger", organizer=_GROUP_CAL,
                  link="https://meet.google.com/bra-kite-ldg",
                  attendees=["harrison@hjrglobal.com", "pal@example.test", mc.LEGACY_NOTETAKER])
        ext["attendees"][1]["responseStatus"] = "accepted"
        ext["attendees"][2]["responseStatus"] = "accepted"
        r3 = _audit({"harrison@hjrglobal.com": [ext]}, [])
        assert len(r3.unconvened) == 1 and r3.misses == []
        # The `self` flag marks the calendar owner even without a roster match.
        assert mc.roster_attendee_accepted(
            [{"attendees": [{"email": "x@y.test", "self": True, "responseStatus": "accepted"}]}],
            frozenset()) is True
        assert mc.roster_attendee_accepted(
            [{"attendees": [{"email": "x@y.test", "responseStatus": "accepted"}]}],
            frozenset()) is False

    def test_unconvened_meetings_carry_their_basis(self):
        """Every re-bucketing carries its reason (the qualify_event doctrine), so a
        reader of the report or ledger can see WHY it was not called a miss."""
        r = _audit({"harrison@hjrglobal.com": _labor_day_events()}, [])
        assert {m.unconvened_basis for m in r.unconvened} == {mc.UNCONVENED_BASIS_GROUP_CALENDAR}

    def test_human_organised_meeting_with_no_transcript_is_still_a_miss(self):
        """The split must not widen: a meeting a PERSON called and nobody captured
        is exactly the gap the auditor exists to report."""
        r = _audit({"harrison@hjrglobal.com": [_ev("evt-1", organizer="harrison@hjrglobal.com")]}, [])
        assert len(r.misses) == 1 and r.unconvened == []
        assert r.misses[0].unconvened_basis == ""

    def test_group_calendar_meeting_with_a_transcript_is_captured_never_unconvened(self):
        """Measured live 2026-09-08: a group-calendar block WAS convened and
        captured. The presumption must never suppress a real capture, and the
        ensure/RSVP lanes may start capturing these series at any time."""
        ev = _ev("gc-1", summary="Velvet Otter Cadence", organizer=_GROUP_CAL,
                 link="https://meet.google.com/vot-cade-nce")
        r = _audit({"harrison@hjrglobal.com": [ev]}, [_t("t1", cal_id="gc-1")])
        assert r.captured == 1
        assert r.unconvened == [] and r.misses == []

    def test_organizer_suffix_match_is_case_insensitive_and_exact(self):
        """A mixed-case organiser address is still a group calendar; a look-alike
        human address that merely CONTAINS the suffix is not."""
        assert mc.presumed_unconvened_basis(
            {"organizer": {"email": "C_ABC@Group.Calendar.Google.Com"}}
        ) == mc.UNCONVENED_BASIS_GROUP_CALENDAR
        assert mc.presumed_unconvened_basis(
            {"organizer": {"email": "group.calendar.google.com@example.test"}}
        ) == ""
        assert mc.presumed_unconvened_basis({"organizer": "not-a-dict"}) == ""
        assert mc.presumed_unconvened_basis({}) == ""

    def test_render_counts_line_names_all_buckets(self):
        """The counts line is what the ops channel reads first. Prevents: the
        unconvened count silently vanishing from the headline."""
        r = _audit({"harrison@hjrglobal.com": _labor_day_events()}, [])
        out = mc.render_report(r)
        assert "5 scheduled, 0 captured, 0 missed, 5 presumed unconvened, 0 duplicated" in out

    def test_render_has_unconvened_section_and_no_not_captured_section(self):
        r = _audit({"harrison@hjrglobal.com": _labor_day_events()}, [])
        out = mc.render_report(r)
        assert "*:white_circle: Presumed unconvened (5)*" in out
        assert "group-calendar blocks, no join evidence" in out
        assert ":red_circle:" not in out
        assert "Velvet Otter Cadence" in out and "Pewter Comet Ledger" in out

    def test_render_clean_text_acknowledges_unconvened(self):
        """'Every scheduled meeting captured exactly once' is FALSE on a day where
        five scheduled meetings were not captured -- and so is a CHECKMARK. The
        pin changed under the D-051 review (C-2): the old line, 'Every convened
        meeting captured exactly once (5 presumed unconvened).', printed a clean
        day the auditor cannot verify (unconvened is a presumption). The day is
        now reported as unverified, with no checkmark anywhere."""
        r = _audit({"harrison@hjrglobal.com": _labor_day_events()}, [])
        out = mc.render_report(r)
        assert ":white_check_mark:" not in out
        assert "5 presumed unconvened -- not verified" in out
        assert "captured exactly once" not in out
        assert "No qualifying roster meetings scheduled" not in out

    def test_clean_text_unchanged_when_nothing_is_unconvened(self):
        """The pre-existing clean line survives byte-for-byte on an ordinary day."""
        r = _audit({"harrison@hjrglobal.com": [_ev("evt-1")]}, [_t("t1", cal_id="evt-1")])
        assert "Every scheduled meeting captured exactly once." in mc.render_report(r)

    def test_unconvened_does_not_mask_a_real_miss(self):
        """The clean criterion excludes unconvened but KEEPS misses: one human-called
        uncaptured meeting beside five placeholders is still a red day."""
        events = _labor_day_events() + [
            _ev("human-1", summary="Amber Heron Compass", hh=13,
                link="https://meet.google.com/amb-hero-cmp", organizer="harrison@hjrglobal.com"),
        ]
        r = _audit({"harrison@hjrglobal.com": events}, [])
        out = mc.render_report(r)
        assert len(r.misses) == 1 and len(r.unconvened) == 5
        assert "*:red_circle: Not captured (1)*" in out
        assert "captured exactly once" not in out

    def test_unconvened_does_not_mask_a_duplicate(self):
        """A duplicated capture beside a placeholder is still a duplicate day."""
        events = [_labor_day_events()[0], _ev("dup-1", link="https://meet.google.com/dup-dup-dup")]
        r = _audit({"harrison@hjrglobal.com": events},
                   [_t("t1", cal_id="dup-1"), _t("t2", cal_id="dup-1")])
        out = mc.render_report(r)
        assert len(r.duplicates) == 1 and len(r.unconvened) == 1
        assert "captured exactly once" not in out

    def test_lex_signal_group_calendar_event_renders_redacted(self):
        """The LEX rail applies to the new section exactly as to misses: a group
        calendar hosting a client-programme block must render as its shape, with
        the organiser withheld and no agency domain on the line."""
        ev = _ev("gc-lex", summary="Quiet Harbor Intake Review", hh=11,
                 link="https://meet.google.com/qui-harb-int", organizer=_GROUP_CAL,
                 attendees=["shaun@lexingtonservices.com", "vreese@azdes.gov"])
        r = _audit({"harrison@hjrglobal.com": [ev]}, [])
        assert len(r.unconvened) == 1
        out = mc.render_report(r)
        assert "Presumed unconvened (1)" in out
        assert "LEX/PHI meeting" in out
        assert "Quiet Harbor" not in out and "intake" not in out.lower()
        assert "withheld" in out
        assert "azdes.gov" not in out and _GROUP_CAL not in out

    def test_unconvened_are_listed_chronologically(self):
        r = _audit({"harrison@hjrglobal.com": _labor_day_events()}, [])
        out = mc.render_report(r)
        assert out.index("09:30") < out.index("10:00") < out.index("17:00")

    def test_audit_ledger_records_unconvened_ids_never_titles(self):
        """The ledger row gains the count + ids only; the existing ids-never-titles
        pin (test_audit_ledger_records_ids_never_titles) must keep holding."""
        text = (_REPO_ROOT / "scripts" / "run_meeting_capture_audit.py").read_text(encoding="utf-8")
        assert "unconvened_event_ids" in text
        assert '"unconvened": len(report.unconvened)' in text
        assert "m.title" not in text and "\"title\"" not in text

    def test_audit_log_line_carries_the_unconvened_count(self):
        text = (_REPO_ROOT / "scripts" / "run_meeting_capture_audit.py").read_text(encoding="utf-8")
        assert "unconvened=%d" in text

    def test_run_marker_outputs_count_unconvened_and_ledger_row_is_ids_only(self, monkeypatch):
        """Behavioural. The registry marks this lane expects_output, and
        run_marker.evaluate WARNs 'FIRED BUT WROTE NOTHING' on outputs==0. Re-bucketing
        the 9/7 five out of `misses` without counting them here would turn the
        fixture day into that false alarm. Also pins the ledger row shape: counts +
        event ids, no titles."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_audit_script", _REPO_ROOT / "scripts" / "run_meeting_capture_audit.py"
        )
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setattr(sys, "argv", ["run_meeting_capture_audit.py", "--day", DAY])
        spec.loader.exec_module(mod)

        report = _audit({"harrison@hjrglobal.com": _labor_day_events()}, [])
        assert len(report.unconvened) == 5   # precondition

        monkeypatch.setattr(mod.mc, "load_config", lambda: _cfg())
        monkeypatch.setattr(mod.mc, "audit_day", lambda day, cfg: report)
        markers: list[dict] = []
        monkeypatch.setattr(mod.run_marker, "write",
                            lambda task, **kw: markers.append(dict(task=task, **kw)))

        assert mod.main() == 0

        assert len(markers) == 1
        assert markers[0]["outputs"] == 5, "unconvened findings must count as output"
        assert markers[0]["detail"] == "scheduled=5 captured=0 missed=0 unconvened=5"

        rows = [json.loads(l) for l in mc.ledger_path().read_text(encoding="utf-8").splitlines()]
        row = [r for r in rows if r.get("lane") == "audit"][-1]
        assert row["missed"] == 0 and row["unconvened"] == 5
        assert sorted(row["unconvened_event_ids"]) == sorted(e["id"] for e in _labor_day_events())
        assert row["missed_event_ids"] == []
        flat = json.dumps(row)
        for title in ("Velvet Otter", "Brass Kite", "Marble Fern", "Copper Lantern", "Pewter Comet"):
            assert title not in flat
# ── RSVP-accept as cora@ (Code #13 slice 3, cq-19b0298cf5be, R1 2026-09-08) ──
#
# The capture identity is guest-added onto a meeting and its invite then sits at
# needsAction forever; nothing in the lane ever answered it. These tests pin the
# accept step on BOTH halves (after every guest-add; a sweep of cora@'s own
# unanswered invites) under the same dual write gate as every other write.

CORA = "cora@hjrglobal.com"
LINK = "https://meet.google.com/rsvp-test-aaa"


def _own(eid, *, status="needsAction", link=LINK, hh=10, extra=(), summary="Weekly Sync",
         organizer="harrison@hjrglobal.com", description=None):
    """cora@'s OWN copy of an event: carries its self-attendee + responseStatus."""
    ev = _ev(eid, link=link, hh=hh, summary=summary, organizer=organizer, description=description)
    ev["attendees"] = (
        [{"email": organizer}]
        + [{"email": e} for e in extra]
        + [{"email": CORA, "responseStatus": status, "self": True}]
    )
    return ev


def _lex_kwargs():
    """An in-domain (guest-addable) organiser plus a client-agency attendee, so the
    display rail redacts deterministically without depending on the classifier."""
    return dict(organizer="ops@lexingtonservices.com",
                attendees=["ops@lexingtonservices.com", "intake@county.gov"])


class _Cal:
    """Stub calendar client surface: records writes, serves cora@'s copy."""

    def __init__(self, monkeypatch, *, own=None, add=(True, "added"), rsvp=(True, "accepted")):
        from cora.tools import calendar_client as cc
        self.cc = cc
        self.adds: list[dict] = []
        self.rsvps: list[dict] = []
        self.copies: list[dict] = []
        self.gets: list[dict] = []
        own = own if own is not None else _own("e1")

        def _add(**k):
            self.adds.append(k)
            if isinstance(add, Exception):
                raise add
            return add

        def _rsvp(**k):
            self.rsvps.append(k)
            if isinstance(rsvp, Exception):
                raise rsvp
            return rsvp

        def _get(**k):
            self.gets.append(k)
            if isinstance(own, Exception):
                raise own
            return own

        monkeypatch.setattr(cc, "add_attendee", _add)
        monkeypatch.setattr(cc, "set_own_response", _rsvp)
        monkeypatch.setattr(cc, "get_event", _get)
        monkeypatch.setattr(cc, "insert_event_copy", lambda **k: self.copies.append(k) or {"id": "c"})


def _guest_add_plan(**ev_over):
    kw = {"link": LINK, "organizer": "harrison@hjrglobal.com", **ev_over}
    return mc.plan_ensure(DAY, _cfg(), list_events=_lister({
        CORA: [],
        "harrison@hjrglobal.com": [_ev("e1", **kw)],
    }))


def _sweep_plan(own_ev, roster_ev=None, *, roster_events=None):
    roster = roster_events if roster_events is not None else [roster_ev]
    return mc.plan_ensure(DAY, _cfg(), list_events=_lister({
        CORA: [own_ev],
        "harrison@hjrglobal.com": roster,
    }))


class TestRsvpPlan:
    def test_guest_add_rows_plan_an_rsvp_and_copy_rows_do_not(self):
        """An RSVP is planned after every guest-add; a copy is ORGANISED by cora@
        and has no invite to answer -- planning one would error every cycle."""
        ga = _guest_add_plan()
        assert [a.rsvp_planned for a in ga.actions if a.action == "guest-add"] == [True]
        cp = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            CORA: [], "harrison@hjrglobal.com": [_ev("e1", link=LINK, organizer="ext@vendor.com")],
        }))
        assert [a.rsvp_planned for a in cp.actions if a.action == "copy"] == [False]

    def test_rows_carry_the_meeting_start_epoch(self):
        """The rsvp ledger row's identity is (link, start) (D-254); without the
        epoch on the action the row could only carry an HH:MM label."""
        res = _guest_add_plan()
        act = [a for a in res.actions if a.action == "guest-add"][0]
        assert act.meeting_start_ts == mc.event_start_ts(_ev("e1", link=LINK)) > 0

    def test_sweep_attaches_the_rsvp_to_the_covered_row_not_a_second_one(self):
        """cora@ hand-invited, still at needsAction, roster copy visible. The RSVP
        rides the meeting's existing 'none' row -- a second row would double the
        qualifying count for one meeting -- and targets cora@'s OWN event id,
        which differs from the roster id for externally-organised meetings."""
        own = _own("_cora-copy", organizer="ext@vendor.com")
        res = _sweep_plan(own, _ev("roster-copy", link=LINK, organizer="ext@vendor.com",
                                   attendees=["harrison@hjrglobal.com", "ext@vendor.com", CORA]))
        rows = [a for a in res.actions if a.action != "skip"]
        assert len(rows) == 1 and res.qualifying == 1
        assert rows[0].action == "none" and rows[0].rsvp_planned is True
        assert rows[0].rsvp_event_id == "_cora-copy" and rows[0].rsvp == ""
        assert rows[0].meeting_link == LINK.lower()

    def test_sweep_ignores_an_already_answered_invite(self):
        """Idempotency at plan time: an accepted (or declined) entry is never a
        candidate, so a healthy calendar plans zero RSVP writes."""
        for status in ("accepted", "declined", "tentative"):
            res = _sweep_plan(_own("c1", status=status), _ev("r1", link=LINK))
            assert not any(a.rsvp_planned or a.rsvp for a in res.actions), status

    def test_sweep_ignores_lane_made_copies(self):
        """A capture copy carries the lane's marker and cora@ ORGANISES it; even
        if its self entry reads needsAction there is nothing to accept."""
        from cora.tools.calendar_client import CAPTURE_COPY_MARKER

        own = _own("copy", description=f"({CAPTURE_COPY_MARKER}) source_event_id: r1")
        res = _sweep_plan(own, _ev("r1", link=LINK, organizer="ext@vendor.com"))
        assert not any(a.rsvp_planned or a.rsvp for a in res.actions)

    def test_sweep_without_a_roster_copy_skips_and_says_so(self):
        """No roster copy means the veto set (carve-outs, declines, [no-bot])
        cannot be evaluated; accepting blind could record a meeting somebody
        opted out of. The safe default is a named skip, never a silent accept."""
        res = _sweep_plan(_own("lonely"), roster_events=[])
        skip = [a for a in res.actions if a.rsvp == "skipped:no-roster-copy"]
        assert len(skip) == 1
        assert skip[0].action == "skip" and skip[0].rsvp_planned is False
        assert skip[0].reason.startswith(mc.RSVP_NO_ROSTER_COPY_REASON)
        assert skip[0].event_id == "lonely" and skip[0].calendar_email == CORA

    def test_sweep_respects_a_veto_on_the_roster_copy(self):
        """CONSENT. Harrison's [no-bot] on his copy vetoes the meeting; cora@'s
        unanswered invite to the same meeting must not be accepted through the
        sweep door either. The veto row is the only row."""
        res = _sweep_plan(_own("c1", summary="Weekly Sync"),
                          _ev("r1", link=LINK, summary="[no-bot] Weekly Sync"))
        assert [a.action for a in res.actions] == ["skip"]
        assert res.actions[0].reason.startswith("title-marker")
        assert not any(a.rsvp_planned or a.rsvp for a in res.actions)

    def test_sweep_skips_when_a_notetaker_is_already_on_the_invite(self):
        """One mechanism per event: accepting on top of an invited notetaker@
        dispatches TWO bots -- the duplicate this build exists to remove."""
        res = _sweep_plan(_own("c1", extra=(mc.LEGACY_NOTETAKER,)),
                          _ev("r1", link=LINK, attendees=["harrison@hjrglobal.com", mc.LEGACY_NOTETAKER]))
        rows = [a for a in res.actions if a.action == "none"]
        assert len(rows) == 1
        assert rows[0].rsvp == "skipped:notetaker-present" and rows[0].rsvp_planned is False

    def test_sweep_plans_the_rsvp_on_a_lex_invite(self):
        """DELIBERATE FLIP (Code #14 R14-4, ruling 9.3 INCLUDE LEX; was
        test_sweep_withholds_a_lex_redacted_invite). A LEX invite with a
        qualifying roster copy plans the accept like any other. The DISPLAY rail is
        untouched: the row still names no organiser and no title."""
        own = _own("c1", **{"organizer": "ops@lexingtonservices.com",
                             "extra": ("intake@county.gov",)})
        assert mc.is_lex_event(own) is True                                # precondition
        res = _sweep_plan(own, _ev("r1", link=LINK, **_lex_kwargs()))
        rows = [a for a in res.actions if a.action == "none"]
        assert len(rows) == 1
        assert rows[0].rsvp == "" and rows[0].rsvp_planned is True
        assert rows[0].title.startswith("LEX/PHI") and "county.gov" not in rows[0].reason

    def test_gov_attendee_on_a_non_lex_meeting_does_not_withhold_the_rsvp(self):
        """Code #13 review C2-1 (the review's failing input): a UFL meeting with a
        city attendee. The DISPLAY rail redacts it (any .gov attendee -- stricter
        by design), but the RSVP withhold is the R1 predicate, `is_lex_event`, and
        this is not a LEX event. Keying the withhold on the redacted title left
        every civic F3E/UFL/HJRP meeting guest-added and never joined."""
        ufl = dict(organizer="one@unitedfightleague.com",
                   attendees=["one@unitedfightleague.com", "parks@phoenix.gov"])
        # guest-add half
        ga = _guest_add_plan(**ufl)
        rows = [a for a in ga.actions if a.action == "guest-add"]
        assert len(rows) == 1
        assert mc.is_lex_event(_ev("e1", link=LINK, **ufl)) is False      # precondition
        assert rows[0].title.startswith("LEX/PHI")                        # display rail untouched
        assert rows[0].rsvp == "" and rows[0].rsvp_planned is True
        # sweep half: cora@'s own unanswered copy of the same meeting
        own = _own("c1", organizer="one@unitedfightleague.com", extra=("parks@phoenix.gov",))
        res = _sweep_plan(own, _ev("r1", link=LINK, **ufl))
        rows = [a for a in res.actions if a.action == "none"]
        assert len(rows) == 1
        assert rows[0].rsvp == "" and rows[0].rsvp_planned is True
        assert "phoenix.gov" not in rows[0].reason

    def test_lex_guest_add_plan_plans_the_rsvp(self):
        """DELIBERATE FLIP (Code #14 R14-4, ruling 9.3; was
        test_lex_guest_add_plan_shows_the_withhold_live_would_apply). Plan mode
        runs the same RSVP planner as the sweep, so a LEX guest-add row reads
        `rsvp=planned` -- plan and live still agree -- and its reason still names
        neither the LEX organiser nor the agency attendee."""
        ga = _guest_add_plan(**_lex_kwargs())
        rows = [a for a in ga.actions if a.action == "guest-add"]
        assert len(rows) == 1
        assert rows[0].rsvp == "" and rows[0].rsvp_planned is True
        assert rows[0].title.startswith("LEX/PHI")
        assert "lexingtonservices" not in rows[0].reason and "county.gov" not in rows[0].reason


class TestRsvpExecute:
    def test_no_rsvp_when_flag_off_even_with_apply(self, monkeypatch):
        cal = _Cal(monkeypatch)
        res = mc.execute_ensure(_guest_add_plan(), _cfg(), apply=True)
        assert res.applied is False and cal.rsvps == [] and cal.adds == []

    def test_no_rsvp_in_plan_mode_with_apply(self, monkeypatch):
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "plan")
        cal = _Cal(monkeypatch)
        res = mc.execute_ensure(_guest_add_plan(), _cfg(), apply=True)
        assert res.applied is False and cal.rsvps == []

    def test_no_rsvp_in_live_mode_without_apply(self, monkeypatch):
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        cal = _Cal(monkeypatch)
        # both halves planned: a guest-add AND a sweep row
        plan = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            CORA: [_own("c2", link="https://meet.google.com/rsvp-test-bbb")],
            "harrison@hjrglobal.com": [
                _ev("e1", link=LINK),
                _ev("r2", link="https://meet.google.com/rsvp-test-bbb"),
            ],
        }))
        assert any(a.rsvp_planned for a in plan.actions if a.action == "none")
        res = mc.execute_ensure(plan, _cfg(), apply=False)
        assert res.applied is False and cal.rsvps == []
        assert all(a.rsvp == "" for a in res.actions)

    def test_guest_add_success_accepts_exactly_once_as_cora(self, monkeypatch):
        """THE R1 STEP. After the guest-add lands, exactly one accept, impersonating
        the capture identity, on the event that was just guest-added."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        cal = _Cal(monkeypatch)
        res = mc.execute_ensure(_guest_add_plan(), _cfg(), apply=True)
        assert len(cal.adds) == 1 and len(cal.rsvps) == 1
        assert cal.rsvps[0] == {"user_email": CORA, "event_id": "e1"}
        act = [a for a in res.actions if a.action == "guest-add"][0]
        assert act.applied is True and act.rsvp == "accepted" and act.error == ""

    def test_already_present_guest_add_still_accepts(self, monkeypatch):
        """The accept is gated on the invite's read-back status, NOT on whether
        add_attendee changed anything -- otherwise an accept that failed last
        cycle is never retried (cora@ is present, so add_attendee is a no-op)."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        cal = _Cal(monkeypatch, add=(False, "already-present"))
        res = mc.execute_ensure(_guest_add_plan(), _cfg(), apply=True)
        assert len(cal.rsvps) == 1
        assert [a.rsvp for a in res.actions if a.action == "guest-add"] == ["accepted"]

    def test_guest_add_fallback_to_copy_never_rsvps(self, monkeypatch):
        """A refused guest-add becomes a copy that cora@ ORGANISES. Accepting on
        it would raise 'not on the guest list' every cycle."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        from cora.tools import calendar_client as cc
        cal = _Cal(monkeypatch, add=cc.CalendarClientError("Calendar HTTP 403 forbiddenForNonOrganizer"))
        res = mc.execute_ensure(_guest_add_plan(), _cfg(), apply=True)
        act = [a for a in res.actions if a.action == "copy"][0]
        assert act.applied is True and len(cal.copies) == 1
        assert cal.rsvps == [] and act.rsvp == "" and act.rsvp_planned is False

    def test_notetaker_present_at_accept_time_skips_without_write(self, monkeypatch):
        """Design v1 s4a: notetaker@ added AFTER the lane's guest-add. The accept
        re-fetches cora@'s copy and turns into a skip -- one mechanism per event."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        cal = _Cal(monkeypatch, own=_own("e1", extra=(mc.LEGACY_NOTETAKER,)))
        res = mc.execute_ensure(_guest_add_plan(), _cfg(), apply=True)
        assert cal.rsvps == []
        assert [a.rsvp for a in res.actions if a.action == "guest-add"] == ["skipped:notetaker-present"]

    def test_lex_event_is_accepted_as_cora_and_labelled_accepted_lex(self, monkeypatch):
        """DELIBERATE FLIP (Code #14 R14-4, ruling 9.3 INCLUDE LEX; was
        test_lex_redacted_event_is_withheld_without_write). A LEX guest-add is
        followed by exactly one accept as cora@, labelled `accepted:lex`."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        cal = _Cal(monkeypatch, own=_own("e1", organizer="ops@lexingtonservices.com",
                                         extra=("intake@county.gov",)))
        res = mc.execute_ensure(_guest_add_plan(**_lex_kwargs()), _cfg(), apply=True)
        act = [a for a in res.actions if a.action == "guest-add"][0]
        assert act.title.startswith("LEX/PHI")                    # display rail untouched
        assert act.applied is True and len(cal.adds) == 1
        assert cal.rsvps == [{"user_email": CORA, "event_id": "e1"}]
        assert act.rsvp == "accepted:lex" and act.rsvp_error == ""

    def test_gov_attendee_non_lex_guest_add_accepts_as_cora(self, monkeypatch):
        """Code #13 review C2-1 at execute time: the UFL + city-attendee meeting is
        redacted on the card but is NOT a LEX event, so the accept goes through
        (one write, as cora@) -- the class the title-keyed check left unanswered."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        ufl = dict(organizer="one@unitedfightleague.com",
                   attendees=["one@unitedfightleague.com", "parks@phoenix.gov"])
        cal = _Cal(monkeypatch, own=_own("e1", organizer="one@unitedfightleague.com",
                                         extra=("parks@phoenix.gov",)))
        res = mc.execute_ensure(_guest_add_plan(**ufl), _cfg(), apply=True)
        act = [a for a in res.actions if a.action == "guest-add"][0]
        assert act.title.startswith("LEX/PHI")                    # display rail untouched
        assert act.applied is True and len(cal.adds) == 1
        assert len(cal.rsvps) == 1 and cal.rsvps[0] == {"user_email": CORA, "event_id": "e1"}
        assert act.rsvp == "accepted" and act.error == ""

    def test_no_bot_copy_is_vetoed_and_never_rsvpd(self, monkeypatch):
        """Veto propagation into the RSVP step: a carve-out on one roster copy
        removes the meeting before either half can act, so neither guest-add
        nor accept ever fires -- even with both write gates open."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        cal = _Cal(monkeypatch)
        plan = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            CORA: [_own("c1")],
            "harrison@hjrglobal.com": [_ev("mine", summary="[no-bot] private", link=LINK)],
            "hannah@hjrglobal.com": [_ev("theirs", summary="Weekly Sync", link=LINK)],
        }))
        res = mc.execute_ensure(plan, _cfg(), apply=True)
        assert res.applied is True
        assert cal.adds == [] and cal.rsvps == [] and cal.copies == []
        assert [a.action for a in res.actions] == ["skip"]

    def test_already_accepted_makes_no_write_and_is_recorded_as_such(self, monkeypatch):
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        cal = _Cal(monkeypatch, add=(False, "already-present"), rsvp=(False, "already-accepted"))
        res = mc.execute_ensure(_guest_add_plan(), _cfg(), apply=True)
        assert len(cal.rsvps) == 1     # the read happened (inside set_own_response); no patch
        assert [a.rsvp for a in res.actions if a.action == "guest-add"] == ["already-accepted"]

    def test_read_back_mismatch_is_recorded_not_raised(self, monkeypatch):
        """A patch that returns 200 but does not take must surface as an rsvp
        error on the row -- never as an accept, and never as an exception that
        stops the rest of the lane or its ledger."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        from cora.tools import calendar_client as cc
        cal = _Cal(monkeypatch, rsvp=cc.CalendarClientError(
            "set_own_response: read-back for e1 shows 'needsAction', expected 'accepted'"))
        res = mc.execute_ensure(_guest_add_plan(), _cfg(), apply=True)
        act = [a for a in res.actions if a.action == "guest-add"][0]
        assert act.rsvp == "error" and "read-back" in act.rsvp_error
        assert act.applied is True and act.error == ""     # the guest-add row itself is clean
        assert len(cal.rsvps) == 1

    def test_own_calendar_fetch_failure_is_an_rsvp_error_not_a_crash(self, monkeypatch):
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        from cora.tools import calendar_client as cc
        cal = _Cal(monkeypatch, own=cc.CalendarClientError("Calendar HTTP 404 fetching event e1"))
        res = mc.execute_ensure(_guest_add_plan(), _cfg(), apply=True)
        act = [a for a in res.actions if a.action == "guest-add"][0]
        assert act.rsvp == "error" and "404" in act.rsvp_error and cal.rsvps == []

    def test_sweep_row_accepts_on_cora_own_event_id(self, monkeypatch):
        """For an externally-organised meeting the id on cora@'s calendar is NOT
        the roster id; accepting on the roster id would 404 (or worse, act on the
        wrong calendar's copy)."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        own = _own("_cora-copy", organizer="ext@vendor.com")
        cal = _Cal(monkeypatch, own=own)
        plan = _sweep_plan(own, _ev("roster-copy", link=LINK, organizer="ext@vendor.com",
                                    attendees=["harrison@hjrglobal.com", CORA]))
        res = mc.execute_ensure(plan, _cfg(), apply=True)
        assert cal.rsvps == [{"user_email": CORA, "event_id": "_cora-copy"}]
        assert cal.adds == [] and cal.copies == []
        assert [a.rsvp for a in res.actions if a.action == "none"] == ["accepted"]

    def test_an_errored_guest_add_row_never_rsvps(self, monkeypatch):
        """If cora@ never reached the guest list there is nothing to accept;
        trying would add a second, misleading error to the same row."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        from cora.tools import calendar_client as cc
        boom = cc.CalendarClientError("nope")
        cal = _Cal(monkeypatch, add=boom, own=boom)
        monkeypatch.setattr(cal.cc, "insert_event_copy", lambda **k: (_ for _ in ()).throw(boom))
        res = mc.execute_ensure(_guest_add_plan(), _cfg(), apply=True)
        assert any(a.error for a in res.actions) and cal.rsvps == []

    def test_second_run_is_a_no_op_write(self, monkeypatch):
        """Idempotent across two consecutive cycles: the second plan sees cora@ on
        the roster copy (action 'none') and cora@'s own entry accepted (no sweep
        candidate), so no accept is even attempted."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        cal = _Cal(monkeypatch)
        mc.execute_ensure(_guest_add_plan(), _cfg(), apply=True)
        assert len(cal.rsvps) == 1
        second = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            CORA: [_own("e1", status="accepted")],
            "harrison@hjrglobal.com": [_ev("e1", link=LINK, attendees=["harrison@hjrglobal.com", CORA])],
        }))
        res = mc.execute_ensure(second, _cfg(), apply=True)
        assert len(cal.rsvps) == 1 and len(cal.adds) == 1
        assert [a.action for a in res.actions] == ["none"] and res.actions[0].rsvp == ""

    def test_rsvp_log_line_carries_link_and_start(self):
        """The kickoff's observability key: `rsvp_accepted` with the (link, start)
        identity, so a fired-but-accepted-nothing run is distinguishable in logs."""
        text = (_REPO_ROOT / "src" / "cora" / "meeting_capture.py").read_text(encoding="utf-8")
        assert "rsvp_accepted event_id=%s link=%s start_ts=%s" in text


class TestRsvpLedger:
    def _load(self, monkeypatch, tmp_path):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_ensure_script_rsvp", _REPO_ROOT / "scripts" / "run_meeting_capture_ensure.py"
        )
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setattr(sys, "argv", ["run_meeting_capture_ensure.py", "--apply"])
        spec.loader.exec_module(mod)
        # AFTER exec_module: the script's load_dotenv(override=True) re-populates
        # from .env (see test_ensure_script_does_nothing_at_all_when_the_flag_is_off).
        ledger = tmp_path / "rsvp-ledger.jsonl"
        monkeypatch.setenv("CORA_MEETING_CAPTURE_LEDGER", str(ledger))
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        return mod, ledger

    @staticmethod
    def _rows(ledger):
        return [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_rsvp_ledger_row_carries_link_and_start_never_a_title(self, monkeypatch, tmp_path):
        """D-254 identity + D-082: the row must let a human find the meeting by
        (link, start) and must never carry the title."""
        mod, ledger = self._load(monkeypatch, tmp_path)
        cal = _Cal(monkeypatch)
        title = "Vermilion Kestrel Planning"
        plan = _guest_add_plan(summary=title)
        monkeypatch.setattr(mc, "plan_ensure", lambda d, c: plan)
        mod._run_day(DAY, _cfg(), apply=True)
        assert len(cal.rsvps) == 1
        rows = self._rows(ledger)
        rsvp = [r for r in rows if r.get("action") == "rsvp-accept"]
        assert len(rsvp) == 1
        r = rsvp[0]
        assert r["outcome"] == "accepted" and r["event_id"] == "e1" and r["calendar"] == CORA
        assert r["meeting_link"] == LINK.lower() and r["start_ts"] == mc.event_start_ts(_ev("e1"))
        assert r["lane"] == "ensure" and r["mode"] == "live" and r["applied"] is True
        assert "title" not in r
        assert title not in ledger.read_text(encoding="utf-8")
        # the guest-add's own action row is still there, unchanged in shape
        ga = [r for r in rows if r.get("action") == "guest-add"]
        assert len(ga) == 1 and ga[0]["was_applied"] is True

    def test_two_consecutive_runs_write_one_rsvp_row(self, monkeypatch, tmp_path):
        """Idempotency the ledger can prove: run 2 finds the entry already accepted,
        performs no write, and adds no rsvp-accept row."""
        mod, ledger = self._load(monkeypatch, tmp_path)
        outcomes = iter([(True, "accepted"), (False, "already-accepted")])
        cal = _Cal(monkeypatch, add=(False, "already-present"))
        monkeypatch.setattr(cal.cc, "set_own_response",
                            lambda **k: cal.rsvps.append(k) or next(outcomes))
        # build BOTH plans before patching the planner (the helper calls it)
        plans = iter([_guest_add_plan(), _guest_add_plan()])
        monkeypatch.setattr(mc, "plan_ensure", lambda d, c: next(plans))
        mod._run_day(DAY, _cfg(), apply=True)
        mod._run_day(DAY, _cfg(), apply=True)
        assert len(cal.rsvps) == 2                    # read both times, wrote once
        rows = self._rows(ledger)
        assert len([r for r in rows if r.get("action") == "rsvp-accept"]) == 1
        assert len([r for r in rows if r.get("row") == "summary"]) == 2

    def test_notetaker_skip_error_and_lex_accept_are_ledgered_but_no_roster_is_not(
            self, monkeypatch, tmp_path):
        mod, ledger = self._load(monkeypatch, tmp_path)
        from cora.tools import calendar_client as cc
        act = lambda **kw: mc.EnsureAction(**{**dict(member="M", calendar_email="m@hjrglobal.com",
                                                     event_id="e", title="t", start_label="10:00",
                                                     action="none", reason="already-covered"), **kw})
        assert mod._rsvp_ledger_worthy(act(rsvp="skipped:notetaker-present"))
        assert mod._rsvp_ledger_worthy(act(rsvp="error", rsvp_error="x"))
        assert mod._rsvp_ledger_worthy(act(rsvp="accepted"))
        # DELIBERATE FLIP (Code #14 R14-4, ruling 9.3): a LEX accept is a real
        # write and is ledgered like any accept, on the re-derived `none` row too.
        assert mod._rsvp_ledger_worthy(act(rsvp="accepted:lex"))
        assert not hasattr(mod, "_RSVP_LEDGERED_ONCE_ON_APPLY")
        assert not mod._rsvp_ledger_worthy(act(rsvp="already-accepted"))
        assert not mod._rsvp_ledger_worthy(act(rsvp="skipped:no-roster-copy"))
        # and the no-roster-copy SKIP row is structural for the action-row predicate too
        assert not mod._action_row_worthy(act(
            action="skip", reason=f"{mc.RSVP_NO_ROSTER_COPY_REASON}: x", rsvp="skipped:no-roster-copy"))
        assert cc is not None

    def test_lex_accept_is_ledgered_once_and_never_carries_a_title(
            self, monkeypatch, tmp_path):
        """DELIBERATE FLIP (Code #14 R14-4, ruling 9.3; was
        test_lex_withheld_is_ledgered_once_on_the_run_whose_guest_add_landed).
        Run 1: the LEX guest-add lands and cora@ accepts -> ONE rsvp-accept row,
        outcome accepted:lex, ids and (link, start) only. Run 2: the invite is
        already accepted -> no write attempted to land, no second row. Neither the
        LEX organiser nor the agency attendee nor the title reaches the ledger."""
        mod, ledger = self._load(monkeypatch, tmp_path)
        outcomes = iter([(True, "accepted"), (False, "already-accepted")])
        title = "Quiet Harbor Intake Review"
        cal = _Cal(monkeypatch, own=_own("e1", organizer="ops@lexingtonservices.com",
                                         extra=("intake@county.gov",), summary=title))
        monkeypatch.setattr(cal.cc, "set_own_response",
                            lambda **k: cal.rsvps.append(k) or next(outcomes))
        plans = iter([_guest_add_plan(summary=title, **_lex_kwargs()),
                      _guest_add_plan(summary=title, **_lex_kwargs())])
        monkeypatch.setattr(mc, "plan_ensure", lambda d, c: next(plans))
        mod._run_day(DAY, _cfg(), apply=True)
        rows = self._rows(ledger)
        rsvp = [r for r in rows if r.get("action") == "rsvp-accept"]
        assert len(rsvp) == 1
        assert rsvp[0]["outcome"] == "accepted:lex" and rsvp[0]["event_id"] == "e1"
        assert rsvp[0]["calendar"] == CORA and "title" not in rsvp[0]
        assert rsvp[0]["meeting_link"] == LINK.lower() and rsvp[0]["start_ts"] > 0
        mod._run_day(DAY, _cfg(), apply=True)
        rows = self._rows(ledger)
        assert len([r for r in rows if r.get("action") == "rsvp-accept"]) == 1
        assert len([r for r in rows if r.get("row") == "summary"]) == 2
        assert len(cal.rsvps) == 2                          # read both times, wrote once
        text = ledger.read_text(encoding="utf-8")
        assert "county.gov" not in text and "lexingtonservices" not in text
        assert title not in text and "Quiet Harbor" not in text

    def test_error_row_carries_the_rsvp_error_not_the_action_error(self, monkeypatch, tmp_path):
        mod, ledger = self._load(monkeypatch, tmp_path)
        from cora.tools import calendar_client as cc
        _Cal(monkeypatch, rsvp=cc.CalendarClientError("read-back shows needsAction"))
        plan = _guest_add_plan()
        monkeypatch.setattr(mc, "plan_ensure", lambda d, c: plan)
        mod._run_day(DAY, _cfg(), apply=True)
        rows = self._rows(ledger)
        rsvp = [r for r in rows if r.get("action") == "rsvp-accept"]
        assert len(rsvp) == 1 and rsvp[0]["outcome"] == "error"
        assert "read-back" in rsvp[0]["error"]
        ga = [r for r in rows if r.get("action") == "guest-add"][0]
        assert ga["error"] == "" and ga["was_applied"] is True


# ── LEX INCLUDED in cora@'s own RSVP (Code #14 R14-4, ruling 2026-09-19 ask 9.3) ──
#
# The withhold is retired for the RSVP ONLY. Every gate that decides whether cora@
# may be on a LEX meeting at all -- carve-outs, veto-on-any-copy, the no-roster-copy
# skip, the notetaker check, the dual write gate, the display redaction -- must
# behave exactly as before on a LEX fixture. One test per gate. Fixtures are
# synthetic (D-145): random-word titles, placeholder addresses.

def _lex_own(eid="e1", **kw):
    return _own(eid, organizer="ops@lexingtonservices.com", extra=("intake@county.gov",), **kw)


def _lex_roster(eid="r1", **kw):
    return _ev(eid, link=LINK, **{**_lex_kwargs(), **kw})


class TestRsvpLexIncluded:
    def test_retired_outcome_is_gone_and_the_label_exists(self):
        """Prevents: the withhold creeping back in under its old name."""
        import inspect

        assert "accepted:lex" in mc.RSVP_OUTCOMES
        assert "skipped:lex-withheld" not in mc.RSVP_OUTCOMES
        for fn in (mc._plan_rsvp, mc._rsvp_accept):
            assert "lex-withheld" not in inspect.getsource(fn), fn.__name__

    def test_sweep_accepts_a_lex_invite_as_cora_under_both_gates(self, monkeypatch):
        """The sweep half: cora@ already on the LEX meeting (hand-invited), roster
        copy visible and qualifying -> one accept, labelled accepted:lex."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        own = _lex_own("_cora-lex")
        cal = _Cal(monkeypatch, own=own)
        plan = _sweep_plan(own, _lex_roster(attendees=["ops@lexingtonservices.com",
                                                      "intake@county.gov", CORA]))
        res = mc.execute_ensure(plan, _cfg(), apply=True)
        assert cal.rsvps == [{"user_email": CORA, "event_id": "_cora-lex"}]
        assert cal.adds == [] and cal.copies == []
        assert [a.rsvp for a in res.actions if a.action == "none"] == ["accepted:lex"]

    def test_a_no_bot_marker_on_one_lex_copy_still_vetoes_the_meeting(self, monkeypatch):
        """Gate: veto-on-any-copy. A [no-bot] on Harrison's copy of a LEX meeting
        kills the meeting for BOTH halves, even with both write gates open."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        cal = _Cal(monkeypatch, own=_lex_own("c1"))
        plan = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            CORA: [_lex_own("c1")],
            "harrison@hjrglobal.com": [_lex_roster("mine", summary="[no-bot] Quiet Harbor")],
            "hannah@hjrglobal.com": [_lex_roster("theirs", summary="Quiet Harbor")],
        }))
        res = mc.execute_ensure(plan, _cfg(), apply=True)
        assert cal.adds == [] and cal.rsvps == [] and cal.copies == []
        assert [a.action for a in res.actions] == ["skip"]
        assert not any(a.rsvp for a in res.actions)

    def test_a_no_record_carve_out_on_a_lex_meeting_still_blocks_both_halves(self, monkeypatch):
        """Gate: qualify_event's no-record carve-outs (domain, title) are evaluated
        before any RSVP planning, LEX or not."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        cal = _Cal(monkeypatch, own=_lex_own("c1"))
        for roster in (
            _lex_roster(attendees=["ops@lexingtonservices.com", "partner@outsidefirm.com"]),
            _lex_roster(summary="Quiet Harbor counsel review"),
        ):
            plan = _sweep_plan(_lex_own("c1"), roster)
            res = mc.execute_ensure(plan, _cfg(), apply=True)
            assert [a.action for a in res.actions] == ["skip"], roster["summary"]
            assert res.actions[0].reason.startswith(("no-record-domain", "no-record-title"))
        assert cal.adds == [] and cal.rsvps == []

    def test_a_lex_invite_with_no_roster_copy_is_still_skipped(self, monkeypatch):
        """Gate: never accept blind. Without a roster copy the veto set cannot be
        evaluated, so a LEX invite is skipped exactly like any other."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        cal = _Cal(monkeypatch, own=_lex_own("lonely"))
        res = mc.execute_ensure(_sweep_plan(_lex_own("lonely"), roster_events=[]),
                                _cfg(), apply=True)
        assert [a.rsvp for a in res.actions] == ["skipped:no-roster-copy"]
        assert res.actions[0].title.startswith("LEX/PHI")
        assert cal.rsvps == [] and cal.adds == []

    def test_notetaker_on_a_lex_meeting_still_skips_at_plan_and_at_execute(self, monkeypatch):
        """Gate: one mechanism per event, both halves. Plan: the sweep sees a bot
        on the LEX invite. Execute: a bot was added AFTER the guest-add."""
        res = _sweep_plan(
            _own("c1", organizer="ops@lexingtonservices.com",
                 extra=("intake@county.gov", mc.LEGACY_NOTETAKER)),
            _lex_roster(attendees=["ops@lexingtonservices.com", "intake@county.gov",
                                   mc.LEGACY_NOTETAKER]))
        rows = [a for a in res.actions if a.action == "none"]
        assert len(rows) == 1 and rows[0].rsvp == "skipped:notetaker-present"
        assert rows[0].rsvp_planned is False
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        cal = _Cal(monkeypatch, own=_own("e1", organizer="ops@lexingtonservices.com",
                                         extra=("intake@county.gov", mc.LEGACY_NOTETAKER)))
        res = mc.execute_ensure(_guest_add_plan(**_lex_kwargs()), _cfg(), apply=True)
        assert cal.rsvps == []
        assert [a.rsvp for a in res.actions if a.action == "guest-add"] == ["skipped:notetaker-present"]

    @pytest.mark.parametrize("mode,apply", [(None, True), ("plan", True), ("live", False)])
    def test_the_dual_write_gate_still_holds_on_a_lex_meeting(self, monkeypatch, mode, apply):
        """Gate: CORA_ONECORA_ENSURE=live AND --apply. Either alone writes nothing,
        LEX included."""
        if mode is None:
            monkeypatch.delenv("CORA_ONECORA_ENSURE", raising=False)
        else:
            monkeypatch.setenv("CORA_ONECORA_ENSURE", mode)
        cal = _Cal(monkeypatch, own=_lex_own("e1"))
        plan = mc.plan_ensure(DAY, _cfg(), list_events=_lister({
            CORA: [_lex_own("c2", link="https://meet.google.com/rsvp-test-bbb")],
            "harrison@hjrglobal.com": [
                _lex_roster("e1"),
                _ev("r2", link="https://meet.google.com/rsvp-test-bbb", **_lex_kwargs()),
            ],
        }))
        assert any(a.rsvp_planned for a in plan.actions)                  # precondition
        res = mc.execute_ensure(plan, _cfg(), apply=apply)
        assert res.applied is False
        assert cal.rsvps == [] and cal.adds == [] and cal.copies == []
        assert all(a.rsvp == "" for a in res.actions)

    def test_display_rail_is_untouched_on_a_lex_meeting(self):
        """Gate: display_title still redacts a LEX meeting to its shape and time."""
        ev = _lex_roster(summary="Quiet Harbor Intake Review")
        assert mc.display_title(ev) == f"LEX/PHI meeting, {mc.event_time_label(ev)}"
        rows = [a for a in _guest_add_plan(summary="Quiet Harbor Intake Review",
                                           **_lex_kwargs()).actions]
        assert rows[0].title == mc.display_title(ev)
        assert rows[0].reason == "in-domain organizer withheld"

    def test_a_classifier_failure_can_only_mislabel_never_withhold(self, monkeypatch):
        """is_lex_event fails SAFE to True. That used to WITHHOLD; now it may only
        LABEL. A non-LEX meeting whose classifier raises is still accepted (the
        write happens), and reads accepted:lex -- never an error, never a skip."""
        from cora.connectors import fireflies_connector as ffc

        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        cal = _Cal(monkeypatch)

        def boom(_):
            raise RuntimeError("classifier down")

        monkeypatch.setattr(ffc, "classify_lex_meeting", boom)
        res = mc.execute_ensure(_guest_add_plan(), _cfg(), apply=True)
        act = [a for a in res.actions if a.action == "guest-add"][0]
        assert cal.rsvps == [{"user_email": CORA, "event_id": "e1"}]
        assert act.rsvp == "accepted:lex" and act.rsvp_error == ""
        # and even an is_lex_event that itself raises cannot turn the accept into
        # an error: the label wrapper fails toward the label, after the write.
        plan2 = _guest_add_plan()   # planned first: display_title also calls is_lex_event
        monkeypatch.setattr(mc, "is_lex_event", lambda ev: (_ for _ in ()).throw(ValueError("x")))
        cal2 = _Cal(monkeypatch)
        res2 = mc.execute_ensure(plan2, _cfg(), apply=True)
        act2 = [a for a in res2.actions if a.action == "guest-add"][0]
        assert len(cal2.rsvps) == 1 and act2.rsvp == "accepted:lex"

    def test_already_accepted_lex_invite_is_not_relabelled_or_rewritten(self, monkeypatch):
        """accepted:lex marks a WRITE. A LEX invite that was already accepted is
        'already-accepted' like any other, so it earns no ledger row."""
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        cal = _Cal(monkeypatch, own=_lex_own("e1"), add=(False, "already-present"),
                   rsvp=(False, "already-accepted"))
        res = mc.execute_ensure(_guest_add_plan(**_lex_kwargs()), _cfg(), apply=True)
        assert [a.rsvp for a in res.actions if a.action == "guest-add"] == ["already-accepted"]
        assert len(cal.rsvps) == 1

    def test_the_rsvp_log_line_keeps_its_pinned_prefix_and_labels_lex(self, monkeypatch, caplog):
        """The observability key (pinned by test_rsvp_log_line_carries_link_and_start)
        keeps its exact prefix; the LEX label rides after it. No title on the line."""
        import logging

        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        _Cal(monkeypatch, own=_lex_own("e1", summary="Quiet Harbor Intake Review"))
        with caplog.at_level(logging.INFO, logger=mc.log.name):
            mc.execute_ensure(_guest_add_plan(summary="Quiet Harbor Intake Review",
                                              **_lex_kwargs()), _cfg(), apply=True)
        lines = [r.getMessage() for r in caplog.records if "rsvp_accepted" in r.getMessage()]
        assert len(lines) == 1
        assert lines[0].startswith("rsvp_accepted event_id=e1 link=") and lines[0].endswith("lex=True")
        assert "Quiet Harbor" not in lines[0] and "county.gov" not in lines[0]


# ── the 07:22 audit counts cora@'s RSVP accepts, LEX apart (R14-4) ──────────────

def _rsvp_rows(ledger_file, rows):
    with ledger_file.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _rr(outcome, *, day=DAY, link=LINK, start=1_000, eid="e1", applied=True, lane="ensure",
        action="rsvp-accept"):
    return {"ts": "2026-08-26T17:00:00+00:00", "lane": lane, "day": day, "mode": "live",
            "applied": applied, "action": action, "outcome": outcome, "event_id": eid,
            "calendar": CORA, "meeting_link": link, "start_ts": start, "error": ""}


class TestRsvpCounts:
    def test_counts_one_day_distinct_meetings_and_lex_apart(self, tmp_path):
        ledger = tmp_path / "l.jsonl"
        _rsvp_rows(ledger, [
            _rr("accepted", link="https://meet.google.com/a-a-a"),
            _rr("accepted:lex", link="https://meet.google.com/b-b-b"),
            _rr("accepted:lex", link="https://meet.google.com/c-c-c"),
            # an error re-ledgered every cycle, then accepted on the sweep (other id)
            _rr("error", link="https://meet.google.com/d-d-d", eid="roster-id"),
            _rr("error", link="https://meet.google.com/d-d-d", eid="roster-id"),
            _rr("accepted", link="https://meet.google.com/d-d-d", eid="_cora-id"),
            # an error never resolved, twice
            _rr("error", link="https://meet.google.com/e-e-e"),
            _rr("error", link="https://meet.google.com/e-e-e"),
            _rr("skipped:notetaker-present", link="https://meet.google.com/f-f-f"),
            # noise that must not count: another day, a plan-mode row, another lane,
            # another action
            _rr("accepted", day="2026-08-27", link="https://meet.google.com/g-g-g"),
            _rr("accepted", applied=False, link="https://meet.google.com/h-h-h"),
            _rr("accepted", lane="audit", link="https://meet.google.com/i-i-i"),
            _rr("accepted", action="guest-add", link="https://meet.google.com/j-j-j"),
        ])
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write("{not json\n\n[1, 2]\n")
        c = mc.rsvp_counts(DAY, path=ledger)
        assert c == {"available": True, "accepted": 2, "accepted_lex": 2,
                     "errors": 1, "notetaker_present": 1}

    def test_default_path_is_the_redirected_ledger_and_a_missing_file_is_zero(self, tmp_path):
        assert mc.rsvp_counts(DAY)["accepted"] == 0
        mc.write_ledger([_rr("accepted:lex")])
        assert mc.rsvp_counts(DAY)["accepted_lex"] == 1

    def test_unreadable_ledger_is_unavailable_never_a_raise(self, tmp_path):
        d = tmp_path / "a-directory"
        d.mkdir()
        c = mc.rsvp_counts(DAY, path=d)
        assert c["available"] is False and c["accepted"] == 0

    def test_counts_never_carry_ids_links_or_titles(self, tmp_path):
        ledger = tmp_path / "l.jsonl"
        _rsvp_rows(ledger, [_rr("accepted:lex")])
        flat = json.dumps(mc.rsvp_counts(DAY, path=ledger))
        assert "meet.google.com" not in flat and "e1" not in flat and "@" not in flat

    def test_render_line_is_informational_below_the_verdict_and_never_vetoes(self):
        """Counts only; after the clean-day line; a clean day stays clean."""
        r = _audit({"harrison@hjrglobal.com": [_ev("evt-1")]}, [_t("t1", cal_id="evt-1")])
        before = mc.render_report(r)
        r.rsvp = {"available": True, "accepted": 3, "accepted_lex": 2, "errors": 1,
                  "notetaker_present": 0}
        out = mc.render_report(r)
        line = "_RSVP as cora@: 5 accepted (2 LEX), 1 unresolved error(s)_"
        assert line in out
        assert "Every scheduled meeting captured exactly once." in out
        assert out.index("captured exactly once") < out.index(line) < out.index(r.seat_note)
        assert out.replace(f"\n\n{line}", "") == before

    def test_render_line_absent_when_unattached_or_unavailable(self):
        r = _audit({"harrison@hjrglobal.com": [_ev("evt-1")]}, [])
        base = mc.render_report(r)
        assert "RSVP as cora@" not in base
        r.rsvp = {"available": False, "accepted": 9, "accepted_lex": 9, "errors": 9}
        assert mc.render_report(r) == base

    def test_render_line_sits_below_the_alarms_on_a_red_day(self):
        r = _audit({"harrison@hjrglobal.com": [_ev("evt-1")]}, [])
        r.rsvp = {"available": True, "accepted": 0, "accepted_lex": 1, "errors": 0}
        out = mc.render_report(r)
        assert out.index(":red_circle:") < out.index("_RSVP as cora@: 1 accepted (1 LEX)")

    def test_audit_script_ledgers_rsvp_counts_and_stays_ids_only(self, monkeypatch, caplog):
        """Behavioural: the 07:22 row carries rsvp_accepted / rsvp_accepted_lex /
        rsvp_errors / rsvp_counts_available, the log line carries them, and no
        title, link or address reaches the row."""
        import importlib.util
        import logging

        spec = importlib.util.spec_from_file_location(
            "_audit_script_rsvp", _REPO_ROOT / "scripts" / "run_meeting_capture_audit.py"
        )
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setattr(sys, "argv", ["run_meeting_capture_audit.py", "--day", DAY])
        spec.loader.exec_module(mod)

        mc.write_ledger([
            _rr("accepted", link="https://meet.google.com/a-a-a"),
            _rr("accepted:lex", link="https://meet.google.com/b-b-b"),
            _rr("error", link="https://meet.google.com/c-c-c"),
        ])
        report = _audit({"harrison@hjrglobal.com": [_ev("evt-1", summary="Vermilion Kestrel")]},
                        [_t("t1", cal_id="evt-1")])
        monkeypatch.setattr(mod.mc, "load_config", lambda: _cfg())
        monkeypatch.setattr(mod.mc, "audit_day", lambda day, cfg: report)
        monkeypatch.setattr(mod.run_marker, "write", lambda task, **kw: None)
        printed: list[str] = []
        monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(" ".join(map(str, a))))
        with caplog.at_level(logging.INFO, logger=mod.log.name):
            assert mod.main() == 0
        logged = [r.getMessage() for r in caplog.records]

        rows = [json.loads(l) for l in mc.ledger_path().read_text(encoding="utf-8").splitlines()]
        row = [r for r in rows if r.get("lane") == "audit"][-1]
        assert row["rsvp_accepted"] == 1 and row["rsvp_accepted_lex"] == 1
        assert row["rsvp_errors"] == 1 and row["rsvp_counts_available"] is True
        flat = json.dumps({k: v for k, v in row.items() if k.startswith("rsvp")})
        assert "meet.google.com" not in flat and "@" not in flat
        assert "Vermilion Kestrel" not in json.dumps(row)
        assert any("rsvp_accepted=1 rsvp_accepted_lex=1 rsvp_errors=1" in m for m in logged)
        assert any("_RSVP as cora@: 2 accepted (1 LEX), 1 unresolved error(s)_" in p for p in printed)


# ── Meet join audit consumer (Code #14 R14-8, ruled 2026-09-19 ask 9.6) ─────────
#
# The Reports read ships DARK: with the lane dark (the conftest default, dark:test,
# or a real dark:scope) every bucket and every rendered byte is today's. When the
# read is LIVE, complete, past the lag floor and the contradiction check passes,
# ONLY presumed-unconvened group-calendar blocks may be re-bucketed (narrow first
# rung). Fixtures are synthetic (D-145): random-word titles, placeholder addresses.

import hashlib as _hashlib  # noqa: E402

from cora.connectors import meet_audit as ma  # noqa: E402

#: 14:00 AZ the day after DAY: day end + 14h, well past the 6h lag floor.
_AFTER = datetime(2026, 8, 27, 14, 0, tzinfo=AZ)
_CTL_LINK = "https://meet.google.com/amb-hero-cmp"


def _mj(link_or_code, *, hh=9, mm=35, ep="ep-a", human=True, minutes=20, cal_id="", day=26,
        person=None):
    """One parsed join. `person` is the identity behind the endpoint: None = a
    distinct person per endpoint (the fixture default), a string = that person
    (two endpoints sharing it are ONE person), "" = no email identity (anonymous)."""
    start = int(datetime(2026, 8, day, hh, mm, tzinfo=AZ).timestamp())
    who = ("person-" + ep) if person is None else person
    return ma.MeetJoin(
        meeting_code=ma.normalize_meeting_code(link_or_code), calendar_event_id=cal_id,
        conference_id="", start_ts=start, end_ts=start + minutes * 60,
        endpoint_key=_hashlib.sha256(ep.encode()).hexdigest()[:12], is_human=human,
        person_key=_hashlib.sha256(who.encode()).hexdigest()[:12] if who else "",
    )


def _read(joins=(), state="live", reason=""):
    return ma.MeetAuditRead(state=state, joins=tuple(joins), events_read=len(joins), reason=reason)


def _with_creator(events, creator="harrison@hjrglobal.com"):
    for ev in events:
        ev["creator"] = {"email": creator}
    return events


def _control():
    """A captured Meet meeting whose join IS in the log -- proves the log present.
    In-domain CREATED (lex-phi-identity-4: only an in-domain-created captured Meet
    is a control; the org log may not record an externally-hosted one)."""
    ev = _ev("ctl-1", summary="Amber Heron Compass", hh=13, link=_CTL_LINK,
             organizer="harrison@hjrglobal.com")
    ev["creator"] = {"email": "harrison@hjrglobal.com"}
    return ev


def _ctl_join():
    return _mj(_CTL_LINK, hh=13, mm=2, ep="ctl-a")


def _audit_meet(events, transcripts, read, *, clock=_AFTER, cfg=None):
    return mc.audit_day(
        DAY, cfg or _cfg(),
        list_events=_lister(events),
        fetch_transcripts=lambda a, b: transcripts,
        fetch_seats=lambda: [{"email": "harrison@hjrglobal.com"}],
        fetch_meet_joins=lambda s, e: read,
        clock=lambda: clock,
    )


def _labor_day_with_control():
    events = _with_creator(_labor_day_events()) + [_control()]
    return {"harrison@hjrglobal.com": events}, [_t("t-ctl", cal_id="ctl-1", hh=13, link=_CTL_LINK)]


class TestMeetJoinAudit:
    def test_dark_lane_is_byte_identical_to_today(self):
        """Ships dark: every existing fixture renders and buckets exactly as the
        pre-R14-8 auditor did, whether the lane is the conftest's dark:test or a
        real dark:scope/api/subject."""
        acc = _ev("gc-acc", summary="Velvet Otter Cadence", organizer=_GROUP_CAL,
                  link="https://meet.google.com/vot-cade-nce",
                  attendees=["harrison@hjrglobal.com", "hannah@hjrglobal.com"])
        acc["attendees"][0]["responseStatus"] = "accepted"
        fixtures = [
            ({"harrison@hjrglobal.com": _labor_day_events()}, []),
            ({"harrison@hjrglobal.com": [acc]}, []),
            ({"harrison@hjrglobal.com": [_ev("evt-1")]}, []),
            ({"harrison@hjrglobal.com": [_ev("evt-1")]}, [_t("t1", cal_id="evt-1")]),
        ]
        for events, transcripts in fixtures:
            base = _audit(events, transcripts)
            assert base.meet_audit_state == "dark:test"
            for state in ("dark:scope", "dark:api", "dark:subject"):
                r = _audit_meet(events, transcripts, _read(state=state))
                assert mc.render_report(r) == mc.render_report(base), state
                assert [m.event_id for m in r.misses] == [m.event_id for m in base.misses]
                assert [m.event_id for m in r.unconvened] == [m.event_id for m in base.unconvened]
                assert r.meet_audit_state == state
        # the pinned Labor-Day headline survives the dark lane byte-for-byte
        assert "5 scheduled, 0 captured, 0 missed, 5 presumed unconvened, 0 duplicated" in \
            mc.render_report(_audit({"harrison@hjrglobal.com": _labor_day_events()}, []))

    def test_labor_day_blocks_with_no_joins_become_confirmed_unconvened(self):
        events, transcripts = _labor_day_with_control()
        r = _audit_meet(events, transcripts, _read([_ctl_join()]))
        assert r.meet_audit_state == "live"
        assert len(r.unconvened) == 5 and r.misses == []
        assert {m.unconvened_basis for m in r.unconvened} == {mc.UNCONVENED_BASIS_MEET_NO_JOIN}
        out = mc.render_report(r)
        assert "6 scheduled, 1 captured, 0 missed, 5 not convened (Meet join log), " \
               "0 presumed unconvened, 0 duplicated" in out
        assert "*:white_circle: Not convened (Meet join log) (5)*" in out
        assert "Presumed unconvened" not in out
        assert ":white_check_mark: Every convened meeting captured exactly once " \
               "(5 not convened, per the Meet join log)." in out

    def test_group_calendar_block_people_joined_is_a_miss(self):
        """Stricter than today: a presumed placeholder that >=2 people joined and
        nothing captured is a MISS above the alarms."""
        events, transcripts = _labor_day_with_control()
        joins = [_ctl_join(),
                 _mj("https://meet.google.com/vot-cade-nce", ep="p1"),
                 _mj("VOTCADENCE", ep="p2", mm=40)]
        r = _audit_meet(events, transcripts, _read(joins))
        assert [m.event_id for m in r.misses] == ["gc-1_20260826T163000Z"]
        assert r.misses[0].convened_basis == mc.CONVENED_BASIS_MEET
        assert r.misses[0].unconvened_basis == ""
        assert len(r.unconvened) == 4
        out = mc.render_report(r)
        assert "*:red_circle: Not captured (1)*" in out
        assert "_[people joined per the Meet join log]_" in out
        assert ":white_check_mark:" not in out

    def test_person_organised_miss_is_never_touched_in_v1(self):
        """Narrow first rung: a meeting a PERSON called stays a MISS even when the
        log shows nobody joined -- that upgrade is the next rung, not this one."""
        events = {"harrison@hjrglobal.com": [
            _ev("evt-1", organizer="harrison@hjrglobal.com", link="https://meet.google.com/abc-defg-hij"),
            _control()]}
        r = _audit_meet(events, [_t("t-ctl", cal_id="ctl-1", hh=13, link=_CTL_LINK)],
                        _read([_ctl_join()]))
        assert [m.event_id for m in r.misses] == ["evt-1"]
        assert r.misses[0].convened_basis == "" and r.unconvened == []

    def test_solo_join_is_confirmed_unconvened_solo(self):
        events, transcripts = _labor_day_with_control()
        r = _audit_meet(events, transcripts,
                        _read([_ctl_join(), _mj("https://meet.google.com/vot-cade-nce", ep="p1")]))
        solo = [m for m in r.unconvened if m.event_id == "gc-1_20260826T163000Z"]
        assert solo[0].unconvened_basis == mc.UNCONVENED_BASIS_MEET_SOLO
        assert "_[one person joined]_" in mc.render_report(r)

    def test_bot_only_join_is_not_convened(self):
        """A notetaker alone in the room is not a meeting: two BOT endpoints never
        convene it."""
        events, transcripts = _labor_day_with_control()
        joins = [_ctl_join(),
                 _mj("https://meet.google.com/vot-cade-nce", ep="bot1", human=False),
                 _mj("https://meet.google.com/vot-cade-nce", ep="bot2", human=False)]
        r = _audit_meet(events, transcripts, _read(joins))
        assert r.misses == []
        gc1 = [m for m in r.unconvened if m.event_id == "gc-1_20260826T163000Z"][0]
        assert gc1.unconvened_basis == mc.UNCONVENED_BASIS_MEET_NO_JOIN

    def test_recurring_code_joined_yesterday_does_not_convene_today(self):
        """A series reuses one Meet code; the key is (code, time window)."""
        events, transcripts = _labor_day_with_control()
        joins = [_ctl_join(),
                 _mj("https://meet.google.com/vot-cade-nce", ep="p1", day=25),
                 _mj("https://meet.google.com/vot-cade-nce", ep="p2", day=25)]
        r = _audit_meet(events, transcripts, _read(joins))
        assert r.misses == []
        assert all(m.unconvened_basis == mc.UNCONVENED_BASIS_MEET_NO_JOIN for m in r.unconvened)

    def test_calendar_event_id_is_a_secondary_match_with_the_instance_suffix_stripped(self):
        events, transcripts = _labor_day_with_control()
        joins = [_ctl_join(),
                 _mj("", ep="p1", cal_id="gc-1_20260826T163000Z"),
                 _mj("", ep="p2", cal_id="gc-1")]
        r = _audit_meet(events, transcripts, _read(joins))
        assert [m.event_id for m in r.misses] == ["gc-1_20260826T163000Z"]

    def test_zoom_link_block_is_untouched(self):
        ev = _ev("gc-zoom", summary="Brass Kite Ledger", organizer=_GROUP_CAL,
                 link=None, location="https://acme.zoom.us/j/123456789")
        ev["creator"] = {"email": "harrison@hjrglobal.com"}
        events = {"harrison@hjrglobal.com": [ev, _control()]}
        r = _audit_meet(events, [_t("t-ctl", cal_id="ctl-1", hh=13, link=_CTL_LINK)],
                        _read([_ctl_join()]))
        zoom = [m for m in r.unconvened if m.event_id == "gc-zoom"]
        assert zoom and zoom[0].unconvened_basis == mc.UNCONVENED_BASIS_GROUP_CALENDAR

    def test_contradiction_disables_every_decision(self):
        """The failing-capable cross-check: a CAPTURED Meet meeting with no join in
        the log means the log is incomplete, so nothing is decided -- not even the
        stricter MISS -- and the report says so in one line."""
        events, transcripts = _labor_day_with_control()
        joins = [_mj("https://meet.google.com/vot-cade-nce", ep="p1"),
                 _mj("https://meet.google.com/vot-cade-nce", ep="p2")]      # no control join
        r = _audit_meet(events, transcripts, _read(joins))
        assert r.meet_audit_state == "contradiction"
        assert r.misses == [] and len(r.unconvened_presumed) == 5 and r.unconvened_confirmed == []
        out = mc.render_report(r)
        assert ":warning: Meet join log read contradiction -- unconvened remain presumptions today" in out
        assert ":white_check_mark:" not in out
        assert "5 presumed unconvened -- not verified" in out

    @pytest.mark.parametrize("state", ["partial", "error"])
    def test_partial_and_error_change_nothing_but_the_warning_line(self, state):
        events, transcripts = _labor_day_with_control()
        dark = _audit_meet(events, transcripts, _read(state="dark:scope"))
        r = _audit_meet(events, transcripts,
                        _read([_ctl_join(), _mj("https://meet.google.com/vot-cade-nce", ep="p1"),
                               _mj("https://meet.google.com/vot-cade-nce", ep="p2")], state=state))
        assert r.meet_audit_state == state
        warn = f"\n\n:warning: Meet join log read {state} -- unconvened remain presumptions today"
        out = mc.render_report(r)
        assert warn in out and out.replace(warn, "") == mc.render_report(dark)

    def test_lag_floor_not_elapsed_changes_nothing(self):
        """Pinned clock, never a formatted live timestamp: 05:00 AZ the next day is
        5h after the day ended, inside the 6h floor."""
        events, transcripts = _labor_day_with_control()
        early = datetime(2026, 8, 27, 5, 0, tzinfo=AZ)
        r = _audit_meet(events, transcripts,
                        _read([_ctl_join(), _mj("https://meet.google.com/vot-cade-nce", ep="p1"),
                               _mj("https://meet.google.com/vot-cade-nce", ep="p2")]),
                        clock=early)
        assert r.meet_audit_state == "lag" and r.misses == [] and len(r.unconvened_presumed) == 5
        just_past = datetime(2026, 8, 27, 6, 0, tzinfo=AZ)
        r2 = _audit_meet(events, transcripts,
                         _read([_ctl_join(), _mj("https://meet.google.com/vot-cade-nce", ep="p1"),
                                _mj("https://meet.google.com/vot-cade-nce", ep="p2")]),
                         clock=just_past)
        assert r2.meet_audit_state == "live" and len(r2.misses) == 1

    def test_no_join_needs_a_control_and_an_in_domain_creator(self):
        """Silence is evidence only where the log can reach: without a same-day
        captured Meet control, or with an external creator (the org's log may not
        record that meeting), zero joins stays a PRESUMPTION. The stricter MISS
        still applies either way."""
        no_ctl = {"harrison@hjrglobal.com": _with_creator(_labor_day_events())}
        r = _audit_meet(no_ctl, [], _read([_mj("https://meet.google.com/bra-kite-ldg", ep="p1"),
                                            _mj("https://meet.google.com/bra-kite-ldg", ep="p2")]))
        assert r.meet_audit_state == "live"
        assert [m.event_id for m in r.misses] == ["gc-2_20260826T163000Z"]
        assert all(m.unconvened_basis == mc.UNCONVENED_BASIS_GROUP_CALENDAR for m in r.unconvened)
        ext = {"harrison@hjrglobal.com":
               _with_creator(_labor_day_events(), creator="host@vendor.example") + [_control()]}
        r2 = _audit_meet(ext, [_t("t-ctl", cal_id="ctl-1", hh=13, link=_CTL_LINK)],
                         _read([_ctl_join()]))
        assert r2.unconvened_confirmed == [] and len(r2.unconvened_presumed) == 5

    def test_a_raising_reader_is_an_error_state_never_a_crash(self):
        events, transcripts = _labor_day_with_control()

        def boom(s, e):
            raise RuntimeError("reports down for pal@example.test")

        r = mc.audit_day(DAY, _cfg(), list_events=_lister(events),
                         fetch_transcripts=lambda a, b: transcripts,
                         fetch_seats=lambda: [], fetch_meet_joins=boom, clock=lambda: _AFTER)
        assert r.meet_audit_state == "error" and r.meet_audit_reason == "RuntimeError"
        assert len(r.unconvened_presumed) == 5

    def test_the_read_window_brackets_the_az_day(self):
        seen: list = []
        mc.audit_day(DAY, _cfg(), list_events=_lister({"harrison@hjrglobal.com": []}),
                     fetch_transcripts=lambda a, b: [], fetch_seats=lambda: [],
                     fetch_meet_joins=lambda s, e: seen.append((s, e)) or _read(state="dark:scope"))
        (s, e), = seen
        assert s == datetime(2026, 8, 25, 23, 0, tzinfo=AZ)
        assert e == datetime(2026, 8, 27, 6, 0, tzinfo=AZ)

    def test_lex_meeting_rendering_still_redacted_under_a_confirmed_bucket(self):
        ev = _ev("gc-lex", summary="Quiet Harbor Intake Review", hh=11,
                 link="https://meet.google.com/qui-harb-int", organizer=_GROUP_CAL,
                 attendees=["shaun@lexingtonservices.com", "vreese@azdes.gov"])
        ev["creator"] = {"email": "harrison@hjrglobal.com"}
        events = {"harrison@hjrglobal.com": [ev, _control()]}
        r = _audit_meet(events, [_t("t-ctl", cal_id="ctl-1", hh=13, link=_CTL_LINK)],
                        _read([_ctl_join()]))
        assert [m.unconvened_basis for m in r.unconvened] == [mc.UNCONVENED_BASIS_MEET_NO_JOIN]
        out = mc.render_report(r)
        assert "Not convened (Meet join log) (1)" in out and "LEX/PHI meeting" in out
        assert "Quiet Harbor" not in out and "azdes.gov" not in out and "withheld" in out

    def test_instance_suffix_regex_is_linear_on_degenerate_inputs(self):
        """D-171: the one regex this consumer adds."""
        for s in ("_" * 40_000, "1" * 40_000, "_20260826T163000" * 2_500,
                  ("_12345678T123456" * 2_500) + "Q"):
            best = float("inf")
            for _ in range(3):   # best of 3: a single run flakes under host load
                t = time.perf_counter()
                mc._base_event_id(s)
                best = min(best, time.perf_counter() - t)
            assert best < 0.2
        assert mc._base_event_id("gc-1_20260826T163000Z") == "gc-1"
        assert mc._base_event_id("gc-1") == "gc-1"

    def test_audit_ledger_new_keys_are_ids_only(self, monkeypatch):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_audit_script_meet", _REPO_ROOT / "scripts" / "run_meeting_capture_audit.py")
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setattr(sys, "argv", ["run_meeting_capture_audit.py", "--day", DAY])
        spec.loader.exec_module(mod)
        events, transcripts = _labor_day_with_control()
        report = _audit_meet(events, transcripts,
                             _read([_ctl_join(), _mj("https://meet.google.com/vot-cade-nce", ep="p1"),
                                    _mj("https://meet.google.com/vot-cade-nce", ep="p2")]))
        monkeypatch.setattr(mod.mc, "load_config", lambda: _cfg())
        monkeypatch.setattr(mod.mc, "audit_day", lambda day, cfg: report)
        markers: list[dict] = []
        monkeypatch.setattr(mod.run_marker, "write", lambda task, **kw: markers.append(kw))
        assert mod.main() == 0
        rows = [json.loads(l) for l in mc.ledger_path().read_text(encoding="utf-8").splitlines()]
        row = [r for r in rows if r.get("lane") == "audit"][-1]
        assert row["meet_audit_state"] == "live" and row["meet_events_read"] == 3
        assert row["convened_event_ids"] == ["gc-1_20260826T163000Z"]
        assert len(row["unconvened_confirmed_event_ids"]) == 4
        assert row["unconvened_presumed_event_ids"] == []
        new = {k: row[k] for k in ("meet_audit_state", "meet_audit_reason", "meet_events_read",
                                   "convened_event_ids", "unconvened_confirmed_event_ids",
                                   "unconvened_presumed_event_ids")}
        flat = json.dumps(new)
        assert "@" not in flat and "meet.google.com" not in flat and "votcadence" not in flat
        assert "Velvet Otter" not in json.dumps(row)
        # the run-marker detail pin is untouched
        assert markers[0]["detail"] == "scheduled=6 captured=1 missed=1 unconvened=4"


_VOT = "https://meet.google.com/vot-cade-nce"
_GC1 = "gc-1_20260826T163000Z"


def _gc1(r):
    return [m for m in r.unconvened + r.misses if m.event_id == _GC1][0]


class TestMeetJoinAuditPeopleNotEndpoints:
    """D-051 lex-phi-identity-2: the lane counts PEOPLE. One person on a laptop and
    a phone, or a drop-and-rejoin, is two call_ended endpoints -- counting those
    turned a presumed-unconvened solo block into a false MISS."""

    def test_one_person_on_two_endpoints_is_solo_not_a_miss(self):
        events, transcripts = _labor_day_with_control()
        joins = [_ctl_join(),
                 _mj(_VOT, ep="laptop", person="solo@hjrglobal.test"),
                 _mj(_VOT, ep="phone", person="solo@hjrglobal.test", mm=40)]
        r = _audit_meet(events, transcripts, _read(joins))
        assert r.meet_audit_state == "live" and r.misses == []
        assert _gc1(r).unconvened_basis == mc.UNCONVENED_BASIS_MEET_SOLO
        assert r.meet_undecided_ids == []
        out = mc.render_report(r)
        assert "Not captured" not in out and "_[one person joined]_" in out

    def test_two_distinct_people_are_still_a_miss(self):
        events, transcripts = _labor_day_with_control()
        joins = [_ctl_join(),
                 _mj(_VOT, ep="a1", person="one@hjrglobal.test"),
                 _mj(_VOT, ep="a2", person="one@hjrglobal.test"),
                 _mj(_VOT, ep="b1", person="two@hjrglobal.test", mm=40)]
        r = _audit_meet(events, transcripts, _read(joins))
        assert [m.event_id for m in r.misses] == [_GC1]
        assert r.misses[0].convened_basis == mc.CONVENED_BASIS_MEET

    @pytest.mark.parametrize("joins", [
        # one identified person + an endpoint with no email identity (a dial-in)
        [_mj(_VOT, ep="laptop", person="solo@hjrglobal.test"), _mj(_VOT, ep="dial", person="")],
        # two anonymous endpoints: one person rejoining, or two people -- unknowable
        [_mj(_VOT, ep="anon1", person=""), _mj(_VOT, ep="anon2", person="", mm=40)],
    ], ids=["person-plus-dial-in", "two-anonymous"])
    def test_endpoints_it_cannot_count_in_people_leave_the_presumption(self, joins):
        """FAIL CLOSED: neither a MISS (it may be one person) nor a confirmed solo
        (it may be two). The presumption stands and the line says why."""
        events, transcripts = _labor_day_with_control()
        r = _audit_meet(events, transcripts, _read([_ctl_join()] + joins))
        assert r.meet_audit_state == "live" and r.misses == []
        gc1 = _gc1(r)
        assert gc1.unconvened_basis == mc.UNCONVENED_BASIS_GROUP_CALENDAR
        assert gc1.convened_basis == ""
        assert r.meet_undecided_ids == [_GC1]
        assert gc1 in r.unconvened_presumed and gc1 not in r.unconvened_confirmed
        out = mc.render_report(r)
        assert "_[joined -- the Meet join log cannot tell one person from two]_" in out
        assert ":white_check_mark:" not in out

    def test_one_anonymous_endpoint_alone_is_solo(self):
        events, transcripts = _labor_day_with_control()
        r = _audit_meet(events, transcripts, _read([_ctl_join(), _mj(_VOT, ep="anon", person="")]))
        assert _gc1(r).unconvened_basis == mc.UNCONVENED_BASIS_MEET_SOLO
        assert r.meet_undecided_ids == []

    def test_bots_never_count_as_people_or_as_unknowns(self):
        events, transcripts = _labor_day_with_control()
        joins = [_ctl_join(),
                 _mj(_VOT, ep="laptop", person="solo@hjrglobal.test"),
                 _mj(_VOT, ep="bot1", human=False, person=""),
                 _mj(_VOT, ep="bot2", human=False)]
        r = _audit_meet(events, transcripts, _read(joins))
        assert _gc1(r).unconvened_basis == mc.UNCONVENED_BASIS_MEET_SOLO and r.misses == []

    def test_classify_convened_truth_table(self):
        p = lambda ep, who: _mj(_VOT, ep=ep, person=who)  # noqa: E731
        assert mc.classify_convened([]) == ("unconvened", mc.UNCONVENED_BASIS_MEET_NO_JOIN)
        assert mc.classify_convened([p("a", "x"), p("b", "x")]) == \
            ("unconvened", mc.UNCONVENED_BASIS_MEET_SOLO)
        assert mc.classify_convened([p("a", "x"), p("b", "y")]) == \
            ("convened", mc.CONVENED_BASIS_MEET)
        assert mc.classify_convened([p("a", "x"), p("b", "y"), p("c", "")]) == \
            ("convened", mc.CONVENED_BASIS_MEET)   # two proven people: the unknown cannot undo it
        assert mc.classify_convened([p("a", "x"), p("c", "")]) == \
            (mc.MEET_BUCKET_UNDECIDED, mc.MEET_BASIS_CANNOT_TELL)
        assert mc.classify_convened([p("c", ""), p("c", "")]) == \
            ("unconvened", mc.UNCONVENED_BASIS_MEET_SOLO)   # the SAME endpoint twice

    def test_the_real_parser_folds_a_rejoin_into_one_person(self):
        """The review's repro, end to end through connectors/meet_audit: two
        call_ended events, one identifier, endpoint_ids ep-1 / ep-2."""
        start = int(datetime(2026, 8, 26, 9, 35, tzinfo=AZ).timestamp())

        def event(endpoint, ident="solo.person@hjrglobal.test", itype="email_address"):
            params = [{"name": "meeting_code", "value": "VOTCADENCE"},
                      {"name": "identifier", "value": ident},
                      {"name": "identifier_type", "value": itype},
                      {"name": "display_name", "value": "Solo Person"},
                      {"name": "endpoint_id", "value": endpoint},
                      {"name": "duration_seconds", "intValue": "600"},
                      {"name": "start_timestamp_seconds", "intValue": str(start)}]
            return {"id": {"time": datetime.fromtimestamp(start + 600, timezone.utc)
                           .strftime("%Y-%m-%dT%H:%M:%S.000Z")},
                    "events": [{"name": "call_ended", "parameters": params}]}

        class _Svc:
            def __init__(self, items):
                self.items = items

            def activities(self):
                return self

            def list(self, **kw):
                return self

            def execute(self, num_retries=0):
                return {"items": self.items}

        read = ma.read_call_ended(datetime(2026, 8, 26, tzinfo=timezone.utc),
                                  datetime(2026, 8, 27, tzinfo=timezone.utc),
                                  service=_Svc([event("ep-1"), event("ep-2")]))
        assert read.state == ma.STATE_LIVE and len(read.joins) == 2
        assert len({j.endpoint_key for j in read.joins}) == 2
        assert len({j.person_key for j in read.joins}) == 1
        assert mc.classify_convened(read.joins) == ("unconvened", mc.UNCONVENED_BASIS_MEET_SOLO)
        # a phone-number identifier is not an email identity: cannot tell
        dial = ma.read_call_ended(datetime(2026, 8, 26, tzinfo=timezone.utc),
                                  datetime(2026, 8, 27, tzinfo=timezone.utc),
                                  service=_Svc([event("ep-1"),
                                                event("ep-2", ident="+15555550100", itype="phone_number")]))
        assert mc.classify_convened(dial.joins) == (mc.MEET_BUCKET_UNDECIDED, mc.MEET_BASIS_CANNOT_TELL)

    def test_audit_ledger_carries_undecided_event_ids_only(self, monkeypatch):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_audit_script_meet_undecided", _REPO_ROOT / "scripts" / "run_meeting_capture_audit.py")
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setattr(sys, "argv", ["run_meeting_capture_audit.py", "--day", DAY])
        spec.loader.exec_module(mod)
        events, transcripts = _labor_day_with_control()
        report = _audit_meet(events, transcripts, _read(
            [_ctl_join(), _mj(_VOT, ep="laptop", person="solo@hjrglobal.test"),
             _mj(_VOT, ep="dial", person="")]))
        monkeypatch.setattr(mod.mc, "load_config", lambda: _cfg())
        monkeypatch.setattr(mod.mc, "audit_day", lambda day, cfg: report)
        monkeypatch.setattr(mod.run_marker, "write", lambda task, **kw: None)
        assert mod.main() == 0
        rows = [json.loads(l) for l in mc.ledger_path().read_text(encoding="utf-8").splitlines()]
        row = [r for r in rows if r.get("lane") == "audit"][-1]
        assert row["meet_undecided_event_ids"] == [_GC1]
        assert _GC1 in row["unconvened_presumed_event_ids"]
        flat = json.dumps(row)
        assert "@" not in json.dumps(row["meet_undecided_event_ids"])
        assert "solo@" not in flat and "hjrglobal.test" not in flat


class TestMeetJoinAuditExternalHost:
    """D-051 lex-phi-identity-4: the org's Meet audit log may not record a meeting
    hosted outside the Workspace -- the premise the zero-join branch already rests
    on. So an externally-created captured Meet is neither a contradiction (its
    missing joins say nothing about this log) nor a control."""

    @staticmethod
    def _external_captured(*, creator="host@vendor.example"):
        ev = _ev("ext-1", summary="Saffron Delta Review", hh=15,
                 link="https://meet.google.com/saf-delt-rvw", organizer="host@vendor.example",
                 attendees=["harrison@hjrglobal.com", "host@vendor.example"])
        if creator is not None:
            ev["creator"] = {"email": creator}
        tr = _t("t-ext", cal_id="ext-1", hh=15, link="https://meet.google.com/saf-delt-rvw",
                organizer="host@vendor.example")
        return ev, tr

    def test_an_external_captured_meet_with_no_join_is_not_a_contradiction(self):
        events, transcripts = _labor_day_with_control()
        ev, tr = self._external_captured()
        events["harrison@hjrglobal.com"].append(ev)
        r = _audit_meet(events, transcripts + [tr], _read([_ctl_join()]))
        assert r.meet_audit_state == "live", r.meet_audit_reason
        assert len(r.unconvened_confirmed) == 5 and r.misses == []
        assert "Meet join log read contradiction" not in mc.render_report(r)

    def test_an_external_captured_meet_is_never_a_control(self):
        """Its join proves nothing about in-domain coverage: with no IN-DOMAIN
        captured control, zero-join blocks stay presumptions (the stricter MISS
        still applies)."""
        events = {"harrison@hjrglobal.com": _with_creator(_labor_day_events())}
        ev, tr = self._external_captured()
        events["harrison@hjrglobal.com"].append(ev)
        ext_join = _mj("https://meet.google.com/saf-delt-rvw", hh=15, mm=2, ep="ext-a")
        r = _audit_meet(events, [tr], _read([ext_join]))
        assert r.meet_audit_state == "live"
        assert r.unconvened_confirmed == [] and len(r.unconvened_presumed) == 5

    def test_a_captured_meet_with_no_creator_is_still_checked(self):
        """Fail closed: only a NAMED external creator exempts a meeting; a captured
        Meet whose event carries no creator and no join is still a contradiction."""
        events, transcripts = _labor_day_with_control()
        ev, tr = self._external_captured(creator=None)
        events["harrison@hjrglobal.com"].append(ev)
        r = _audit_meet(events, transcripts + [tr], _read([_ctl_join()]))
        assert r.meet_audit_state == "contradiction"

    def test_an_in_domain_captured_meet_with_no_join_is_still_a_contradiction(self):
        events, transcripts = _labor_day_with_control()
        ev, tr = self._external_captured(creator="hannah@hjrglobal.com")
        events["harrison@hjrglobal.com"].append(ev)
        r = _audit_meet(events, transcripts + [tr], _read([_ctl_join()]))
        assert r.meet_audit_state == "contradiction"


class TestMeetJoinAuditHealthCheck:
    @staticmethod
    def _nhc():
        sys.path.insert(0, str(_REPO_ROOT / "scripts"))
        import nightly_health_check as nhc  # noqa: PLC0415
        return nhc

    def _row(self, **kw):
        base = {"ts": "2026-08-27T14:22:00+00:00", "lane": "audit", "day": DAY,
                "scheduled": 1, "captured": 1, "missed": 0}
        base.update(kw)
        mc.write_ledger([base])

    def test_no_row_and_pre_lane_row_are_info(self):
        nhc = self._nhc()
        r = nhc.check_meet_join_audit()
        assert r.status == "ok" and r.detail.startswith("INFO")
        self._row()
        r = nhc.check_meet_join_audit()
        assert r.status == "ok" and "predates the lane" in r.detail

    @pytest.mark.parametrize("state", ["dark:scope", "dark:api", "dark:subject", "dark:test"])
    def test_dark_is_info_not_a_warn(self, state):
        nhc = self._nhc()
        self._row(meet_audit_state=state, meet_audit_reason="scope not in the DWD grant")
        r = nhc.check_meet_join_audit()
        assert r.status == "ok" and r.detail.startswith("INFO") and state in r.detail

    def test_live_is_ok_with_counts(self):
        nhc = self._nhc()
        self._row(meet_audit_state="live", meet_events_read=7,
                  unconvened_confirmed_event_ids=["a", "b"], convened_event_ids=["c"],
                  unconvened_presumed_event_ids=[])
        r = nhc.check_meet_join_audit()
        assert r.status == "ok" and "live: 7" in r.detail and "2 confirmed unconvened" in r.detail

    @pytest.mark.parametrize("state", ["error", "partial", "contradiction", "weird"])
    def test_error_partial_contradiction_warn(self, state):
        nhc = self._nhc()
        self._row(meet_audit_state="live")
        self._row(meet_audit_state=state, meet_audit_reason="http 500")
        r = nhc.check_meet_join_audit()
        assert r.status == "warn" and state in r.detail and "presumption" in r.detail

    def test_registered_once_right_after_the_catchup_check(self):
        body = (_REPO_ROOT / "scripts" / "nightly_health_check.py").read_text(encoding="utf-8")
        a = "all_results.append(check_missed_nightly_catchup())\n"
        b = "all_results.append(check_meet_join_audit())"
        assert body.count(b) == 1
        i = body.index(a) + len(a)
        assert body[i:].lstrip(" ").startswith(b)


# ── COPA carve-out (ruled by Harrison 2026-09-23, Code #14 close-out ask 1b) ────

class TestCopaCarveOut:
    """COPA (NDA'd deal) meetings are never recorded. The KB already excludes COPA
    transcripts by whole-word title; since R14-4 cora@ also RSVPs to LEX invites
    and its Fireflies seat joins what it accepts, so the carve-out must hold on
    BOTH halves of the ensure lane (guest-add and cora@'s own RSVP). Every case
    reads the REAL roster's patterns -- the data edit is what is under test."""

    COPA_TITLES = (
        "LBHS COPA Diligence",
        "copa sync",
        "COPA-review",
        "Weekly (COPA) update",
        "Re: COPA",
        "COPA's data room walkthrough",
    )
    LOOKALIKES = ("Copacabana offsite", "copay reconciliation", "Copacetic retro")

    @staticmethod
    def _real_patterns() -> tuple[str, ...]:
        return mc.load_config(force=True).no_record_title_patterns

    def test_the_real_roster_carries_copa(self):
        assert "copa" in self._real_patterns()

    @pytest.mark.parametrize("title", COPA_TITLES)
    def test_a_copa_titled_meeting_is_not_recorded(self, title):
        q = mc.qualify_event(_ev("e1", summary=title), mc.load_config(force=True))
        assert not q.qualifies and q.reason == "no-record-title:copa", (title, q)

    @pytest.mark.parametrize("title", LOOKALIKES)
    def test_whole_word_semantics_keep_lookalikes_recorded(self, title):
        assert mc.qualify_event(_ev("e1", summary=title), mc.load_config(force=True)).qualifies, title

    def test_neither_half_of_the_ensure_lane_acts_on_a_copa_lex_meeting(self, monkeypatch):
        monkeypatch.setenv("CORA_ONECORA_ENSURE", "live")
        cfg = _cfg(no_record_title_patterns=self._real_patterns())
        title = "LBHS COPA Diligence"
        cal = _Cal(monkeypatch, own=_lex_own("c1", summary=title))
        # guest-add half: only the roster copy exists; cora@ is not on it yet
        plan = mc.plan_ensure(DAY, cfg, list_events=_lister({
            CORA: [], "harrison@hjrglobal.com": [_lex_roster("r1", summary=title)]}))
        res = mc.execute_ensure(plan, cfg, apply=True)
        assert [a.action for a in res.actions] == ["skip"]
        assert res.actions[0].reason == "no-record-title:copa"
        # RSVP half: the invite already sits on cora@'s calendar at needsAction
        plan = mc.plan_ensure(DAY, cfg, list_events=_lister({
            CORA: [_lex_own("c1", summary=title)],
            "harrison@hjrglobal.com": [_lex_roster("r1", summary=title)]}))
        res = mc.execute_ensure(plan, cfg, apply=True)
        assert [a.action for a in res.actions] == ["skip"]
        assert res.actions[0].reason == "no-record-title:copa"
        assert not any(a.rsvp_planned for a in res.actions)
        assert cal.adds == [] and cal.rsvps == [] and cal.copies == []


class TestCarvedRecordingNeverPrintsItsTitle:
    """D-051 r4 (copa-audit-fallthrough-leak, confirmed 2/2): a COPA meeting recorded
    by a HUMAN's Fireflies seat whose transcript did not join the FIRST vetoing copy
    (cal_id naming another invitee's copy, or no cal_id and a drifted link) fell
    through to 'captured but not on a roster calendar' and printed its full title and
    external organiser to #founder-operations, with the breach alarm silent. The
    breach join now covers EVERY copy, and a transcript whose own title carries a
    title carve-out is a breach by itself -- always rendered as a shape."""

    TITLE = "Copa Health diligence call"
    ORG = "ceo@copahealth.example"
    LINK = "https://zoom.us/j/1234567890"

    def _cfg(self):
        return _cfg(no_record_title_patterns=mc.load_config(force=True).no_record_title_patterns)

    def _two_copies(self):
        kw = dict(summary=self.TITLE, organizer=self.ORG, link=None, location=self.LINK,
                  attendees=[self.ORG, "harrison@hjrglobal.com", "hannah@hjrglobal.com"])
        return {"harrison@hjrglobal.com": [_ev("h_native", **kw)],
                "hannah@hjrglobal.com": [_ev("_hannahcopy", **kw)]}

    def _assert_breach_not_leak(self, r):
        assert len(r.carve_out_breaches) == 1, r.carve_out_breaches
        assert r.carve_out_breaches[0][1] == "no-record-title:copa"
        assert r.unmatched_transcripts == []
        out = mc.render_report(r)
        assert "RECORDED DESPITE A CARVE-OUT" in out
        assert "Copa Health" not in out and self.ORG not in out and "copahealth" not in out
        assert "captured exactly once" not in out

    @pytest.mark.parametrize("cal_id,link", [
        ("_hannahcopy", ""),                       # the OTHER copy's id, no link
        ("", "https://zoom.us/j/1234567890?pwd=zz"),  # no id, drifted link
        ("", ""),                                  # nothing to join on at all
    ])
    def test_a_recorded_copa_meeting_is_a_breach_never_a_titled_unmatched_row(self, cal_id, link):
        t = _t("t1", title=self.TITLE, cal_id=cal_id or None, link=link, organizer=self.ORG)
        self._assert_breach_not_leak(_audit(self._two_copies(), [t], cfg=self._cfg()))

    def test_the_breach_join_matches_any_copy_id_even_without_the_title(self):
        """A transcript renamed in Fireflies still joins by the second copy's id."""
        t = _t("t1", title="Diligence call", cal_id="_hannahcopy", link="", organizer=self.ORG)
        r = _audit(self._two_copies(), [t], cfg=self._cfg())
        assert len(r.carve_out_breaches) == 1 and r.unmatched_transcripts == []
        assert "Diligence call" not in mc.render_report(r)

    def test_a_copa_recording_on_no_roster_calendar_is_a_breach(self):
        t = _t("t1", title="COPA sync", cal_id=None, link="https://zoom.us/j/9", organizer=self.ORG)
        r = _audit({"harrison@hjrglobal.com": []}, [t], cfg=self._cfg())
        self._assert_breach_not_leak(r)

    def test_a_marker_titled_recording_is_a_breach_too(self):
        t = _t("t1", title="[no-bot] private chat", cal_id=None, link="https://zoom.us/j/7")
        r = _audit({"harrison@hjrglobal.com": []}, [t], cfg=self._cfg())
        assert [reason for _s, reason in r.carve_out_breaches] == ["title-marker:[no-bot]"]
        assert "private chat" not in mc.render_report(r)

    def test_an_ordinary_unmatched_capture_still_lists_its_title(self):
        """Control: the belt fires on carve-out titles only."""
        t = _t("t1", title="Vendor walkthrough", cal_id=None, link="https://zoom.us/j/5",
               organizer="rep@vendor.example")
        r = _audit({"harrison@hjrglobal.com": []}, [t], cfg=self._cfg())
        assert r.carve_out_breaches == []
        assert [u["title"] for u in r.unmatched_transcripts] == ["Vendor walkthrough"]

    def test_qualify_event_and_the_belt_share_one_matcher(self):
        cfg = self._cfg()
        for title in ("LBHS COPA Diligence", "[no-bot] x", "Copacabana offsite", "Call with counsel"):
            q = mc.qualify_event(_ev("e1", summary=title), cfg)
            assert (q.reason if not q.qualifies else "") == mc.title_carve_out_reason(title, cfg), title


class TestCarveOutJoinsRoundThree:
    """D-051 r4 re-review of bc61bc4 (all CONFIRMED 2/2): bc61-F1 a marker on ONE copy
    left the other copies' plain title on the recording -> printed; bc61-F2 the link
    fallback bound a recorded carved call to ANOTHER meeting on the same static room
    link, even with the carved event's exact cal_id; bc61-F3 a false breach from a
    link shared with ordinary meetings; bc61-F4 a roster decline raised the breach
    alarm. The rule now: exact carved id before any fallback; a link/title shared with
    a qualifying meeting binds neither side; consent carve-outs only."""

    ROOM = "https://zoom.us/my/harrisonroom"
    ORG = "rep@outside.example"

    def _cfg(self):
        return _cfg(no_record_title_patterns=mc.load_config(force=True).no_record_title_patterns)

    def test_f1_a_marker_on_one_copy_still_catches_the_plain_titled_recording(self):
        kw = dict(organizer=self.ORG, link=None, location="https://zoom.us/j/555",
                  attendees=[self.ORG, "harrison@hjrglobal.com", "hannah@hjrglobal.com"])
        events = {"harrison@hjrglobal.com": [_ev("h1", summary="[no-bot] Diligence call", **kw)],
                  "hannah@hjrglobal.com": [_ev("_hannahcopy", summary="Diligence call", **kw)]}
        t = _t("t1", title="Diligence call", cal_id=None, link="https://zoom.us/j/555?pwd=zz",
               organizer=self.ORG)
        r = _audit(events, [t], cfg=self._cfg())
        assert [reason for _s, reason in r.carve_out_breaches] == ["title-marker:[no-bot]"]
        assert r.unmatched_transcripts == []
        out = mc.render_report(r)
        assert "Diligence call" not in out and self.ORG not in out

    def _room_day(self):
        return {"harrison@hjrglobal.com": [
            _ev("copa1", summary="COPA sync", hh=10, link=self.ROOM),
            _ev("vend1", summary="Vendor walkthrough", hh=14, link=self.ROOM),
        ]}

    def test_f2_an_exact_carved_cal_id_is_a_breach_before_the_link_fallback(self):
        t = _t("t1", title="COPA sync", hh=10, cal_id="copa1", link=self.ROOM)
        r = _audit(self._room_day(), [t], cfg=self._cfg())
        assert [reason for _s, reason in r.carve_out_breaches] == ["no-record-title:copa"]
        assert r.captured == 0 and len(r.misses) == 1          # the vendor call was NOT recorded
        out = mc.render_report(r)
        assert "captured exactly once" not in out and "COPA sync" not in out

    def test_f2_a_no_id_carved_recording_on_a_shared_room_link_is_a_breach(self):
        t = _t("t1", title="COPA sync", hh=10, cal_id=None, link=self.ROOM)
        r = _audit(self._room_day(), [t], cfg=self._cfg())
        assert len(r.carve_out_breaches) == 1 and r.captured == 0

    def test_f2_the_ordinary_meeting_on_the_shared_link_still_joins_by_title(self):
        t = _t("t2", title="Vendor walkthrough", hh=14, cal_id=None, link=self.ROOM)
        r = _audit(self._room_day(), [t], cfg=self._cfg())
        assert r.carve_out_breaches == [] and r.captured == 1 and r.misses == []

    def test_f3_a_generic_titled_recording_on_a_link_shared_with_ordinary_meetings_is_not_a_breach(self):
        """The reviewer's shape: a carved call plus TWO ordinary meetings on one room
        link (so the main link index already drops it as ambiguous); the 14:00
        recording carries a Fireflies-generated title and no cal_id."""
        events = {"harrison@hjrglobal.com": [
            _ev("c1", summary="Call with counsel", hh=10, link=self.ROOM),
            _ev("o1", summary="Vendor walkthrough", hh=14, link=self.ROOM),
            _ev("o2", summary="Team sync", hh=16, link=self.ROOM),
        ]}
        t = _t("t2", title="Harrison's Personal Meeting Room", hh=14, cal_id=None, link=self.ROOM)
        r = _audit(events, [t], cfg=self._cfg())
        assert r.carve_out_breaches == []

    def test_a_link_shared_only_by_carved_meetings_is_a_breach_naming_both_times(self):
        events = {"harrison@hjrglobal.com": [
            _ev("c1", summary="Call with counsel", hh=10, link=self.ROOM),
            _ev("c2", summary="COPA sync", hh=15, link=self.ROOM),
        ]}
        t = _t("t1", title="Harrison's Personal Meeting Room", hh=10, cal_id=None, link=self.ROOM)
        r = _audit(events, [t], cfg=self._cfg())
        assert r.carve_out_breaches == [("a meeting at 10:00 or 15:00",
                                         "no-record-title:copa, no-record-title:counsel")]
        assert r.unmatched_transcripts == []

    def test_f4_refuted_a_recorded_meeting_a_roster_member_declined_is_still_a_breach(self):
        """bc61-F4 was REFUTED 0/2: a decline is a consent signal (qualify_event says
        so), and the link path already alarmed on it. Pinned so the alarm stays."""
        kw = dict(summary="Acme pitch", organizer=self.ORG, link=None, location="https://zoom.us/j/777")
        mine = _ev("h1", **kw)
        mine["attendees"] = [{"email": self.ORG},
                             {"email": "harrison@hjrglobal.com", "self": True, "responseStatus": "declined"}]
        theirs = _ev("_hannahcopy", **kw)
        t = _t("t1", title="Acme pitch", cal_id="_hannahcopy", link="https://zoom.us/j/777?pwd=q",
               organizer=self.ORG)
        r = _audit({"harrison@hjrglobal.com": [mine], "hannah@hjrglobal.com": [theirs]}, [t],
                   cfg=self._cfg())
        assert [reason for _s, reason in r.carve_out_breaches] == ["roster-user-declined"]
        assert "Acme pitch" not in mc.render_report(r)

    def test_a_structural_veto_is_never_a_breach(self):
        """A cancelled copy is not a ruling that the meeting must not be recorded."""
        ev = _ev("x1", summary="Weekly Sync", status="cancelled", link=self.ROOM)
        t = _t("t1", title="Weekly Sync", cal_id="x1", link=self.ROOM)
        r = _audit({"harrison@hjrglobal.com": [ev]}, [t], cfg=self._cfg())
        assert r.carve_out_breaches == []

    def test_a_consent_carve_out_on_any_copy_names_the_meeting_even_after_a_decline(self):
        kw = dict(organizer=self.ORG, link=None, location="https://zoom.us/j/888")
        mine = _ev("h1", summary="Acme pitch", **kw)
        mine["attendees"] = [{"email": self.ORG},
                             {"email": "harrison@hjrglobal.com", "self": True, "responseStatus": "declined"}]
        theirs = _ev("_hannahcopy", summary="[no-bot] Acme pitch", **kw)
        t = _t("t1", title="Acme pitch", cal_id="_hannahcopy", link="", organizer=self.ORG)
        r = _audit({"harrison@hjrglobal.com": [mine], "hannah@hjrglobal.com": [theirs]}, [t],
                   cfg=self._cfg())
        assert [reason for _s, reason in r.skipped] == ["title-marker:[no-bot]"]
        assert [reason for _s, reason in r.carve_out_breaches] == ["title-marker:[no-bot]"]

    def test_the_belt_covers_a_no_record_attendee_address(self):
        t = _t("t1", title="Quarterly review", cal_id=None, link="https://zoom.us/j/4",
               organizer="lawyer@outsidefirm.com")
        r = _audit({"harrison@hjrglobal.com": []}, [t], cfg=self._cfg())
        assert [reason for _s, reason in r.carve_out_breaches] == ["no-record-email:lawyer@outsidefirm.com"]
        assert "Quarterly review" not in mc.render_report(r)
