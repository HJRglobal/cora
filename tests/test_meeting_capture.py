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
    return datetime(2026, 8, 26, hh, mm, tzinfo=AZ).isoformat()


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

    def test_lex_title_never_appears_in_the_report(self):
        ev = _ev("e1", summary="Bob Smith intake assessment",
                 organizer="shaun@lexingtonservices.com", attendees=["shaun@lexingtonservices.com"])
        r = _audit({"harrison@hjrglobal.com": [ev]}, [])
        out = mc.render_report(r)
        assert "Bob Smith" not in out and "intake" not in out.lower()
        assert "LEX/PHI meeting" in out

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
                          "delete_event", "upsert_documents"):
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

        monkeypatch.delenv("CORA_ONECORA_ENSURE", raising=False)
        spec = importlib.util.spec_from_file_location(
            "_ensure_script", _REPO_ROOT / "scripts" / "run_meeting_capture_ensure.py"
        )
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setattr(sys, "argv", ["run_meeting_capture_ensure.py", "--apply"])
        spec.loader.exec_module(mod)

        called: list[str] = []
        monkeypatch.setattr(mc, "plan_ensure", lambda *a, **k: called.append("plan"))
        monkeypatch.setattr(mc, "load_config", lambda *a, **k: called.append("cfg"))
        assert mod.main() == 0
        assert called == [], "the ensure lane must not even load config when off"

    def test_slack_post_goes_through_the_egress_boundary(self):
        """B1 doctrine. The CI guard is only an either/or tripwire, so pin the
        explicit sanitize call here."""
        text = (_REPO_ROOT / "scripts" / "run_meeting_capture_audit.py").read_text(encoding="utf-8")
        assert "sanitize_text" in text and "normalize_slack_bold" in text
        assert "mc.OPS_CHANNEL" in text

    def test_ops_channel_is_pinned_by_id(self):
        assert mc.OPS_CHANNEL == "C0BCUBUDHAR"
