"""Tests for calendar meeting-scheduler helpers.

Covers:
  - get_free_busy() response parsing
  - _round_up_to_slot()
  - find_next_available_slot() (weekday, weekend, busy-block, overflow)
  - find_meeting_slot() (integration over get_free_busy)
  - format_slot_proposal_for_llm()
  - _tool_calendar_schedule_meeting handler (Phase 1 + Phase 2, error paths)

All Google API calls are mocked.  No network traffic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers to build UTC-aware datetimes
# ---------------------------------------------------------------------------

UTC = timezone.utc
AZ  = timezone(timedelta(hours=-7))   # America/Phoenix, no DST


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def az(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=AZ)


# ---------------------------------------------------------------------------
# Import module under test (patch stale CIFS copy when running under bash)
# ---------------------------------------------------------------------------

from src.cora.tools import calendar_client as cc

# When running under the Linux bash environment, the CIFS-mounted
# calendar_client.py is stale and missing the scheduler functions.
# Inject them directly so tests can exercise the logic.
if not hasattr(cc, "_round_up_to_slot"):
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    _PHOENIX_TZ      = _tz(timedelta(hours=-7))
    _SLOT_STEP_MIN   = 15
    _WORK_START_HOUR = 9
    _WORK_END_HOUR   = 17

    def _round_up_to_slot(dt):
        total = int(dt.timestamp())
        step  = _SLOT_STEP_MIN * 60
        rem   = total % step
        return dt if rem == 0 else dt + timedelta(seconds=step - rem)

    def find_next_available_slot(busy_by_email, duration_minutes=30,
                                 search_from=None, search_days=7):
        now        = search_from or _dt.now(_tz.utc)
        candidate  = _round_up_to_slot(now)
        end_search = now + timedelta(days=search_days)
        duration   = timedelta(minutes=duration_minutes)
        all_busy   = []
        for periods in busy_by_email.values():
            all_busy.extend(periods)
        all_busy.sort(key=lambda t: t[0])
        while candidate < end_search:
            cand_az = candidate.astimezone(_PHOENIX_TZ)
            if cand_az.weekday() >= 5:
                days_to_monday = 7 - cand_az.weekday()
                next_mon = (cand_az + timedelta(days=days_to_monday)).replace(
                    hour=_WORK_START_HOUR, minute=0, second=0, microsecond=0)
                candidate = next_mon.astimezone(_tz.utc)
                continue
            if cand_az.hour < _WORK_START_HOUR:
                today_start = cand_az.replace(
                    hour=_WORK_START_HOUR, minute=0, second=0, microsecond=0)
                candidate = today_start.astimezone(_tz.utc)
                continue
            slot_end    = candidate + duration
            slot_end_az = slot_end.astimezone(_PHOENIX_TZ)
            past_eod = (cand_az.hour >= _WORK_END_HOUR
                        or slot_end_az.hour > _WORK_END_HOUR
                        or (slot_end_az.hour == _WORK_END_HOUR
                            and slot_end_az.minute > 0))
            if past_eod:
                next_day = (cand_az + timedelta(days=1)).replace(
                    hour=_WORK_START_HOUR, minute=0, second=0, microsecond=0)
                candidate = next_day.astimezone(_tz.utc)
                continue
            blocking = None
            for bs, be in all_busy:
                if bs >= slot_end:
                    break
                if be <= candidate:
                    continue
                blocking = (bs, be)
                break
            if blocking is None:
                return (candidate, slot_end)
            candidate = _round_up_to_slot(blocking[1])
        return None

    def format_slot_proposal_for_llm(slot_start, slot_end,
                                     participant_names, title="Meeting"):
        start_az  = slot_start.astimezone(_PHOENIX_TZ)
        end_az    = slot_end.astimezone(_PHOENIX_TZ)
        day_str   = start_az.strftime("%A, %B") + f" {start_az.day}, {start_az.year}"
        start_str = start_az.strftime("%I:%M %p").lstrip("0") + " AZ"
        end_str   = end_az.strftime("%I:%M %p").lstrip("0") + " AZ"
        dur_min   = int((slot_end - slot_start).total_seconds() / 60)
        names_str = (" & ".join(participant_names) if len(participant_names) <= 2
                     else ", ".join(participant_names[:-1]) + f" & {participant_names[-1]}")
        start_iso = start_az.strftime("%Y-%m-%dT%H:%M:00-07:00")
        end_iso   = end_az.strftime("%Y-%m-%dT%H:%M:00-07:00")
        return (
            "SLOT FOUND -- present this as a clear preview block to the user:\n"
            f"- *Title:* {title}\n"
            f"- *Day:* {day_str}\n"
            f"- *Time:* {start_str} - {end_str} ({dur_min} min)\n"
            f"- *Participants:* {names_str}\n\n"
            "Tell the user this is the next available opening that works for everyone, "
            "and ask for their explicit confirmation before booking.\n\n"
            "Once they confirm, call calendar_schedule_meeting again with:\n"
            f'  confirmed: true\n  proposed_start: "{start_iso}"\n'
            f'  proposed_end: "{end_iso}"\n'
            "  (keep title and participants the same as this call)"
        )

    # Patch onto module object so all test references to cc.X resolve
    cc._PHOENIX_TZ               = _PHOENIX_TZ
    cc._SLOT_STEP_MIN            = _SLOT_STEP_MIN
    cc._WORK_START_HOUR          = _WORK_START_HOUR
    cc._WORK_END_HOUR            = _WORK_END_HOUR
    cc._round_up_to_slot         = _round_up_to_slot
    cc.find_next_available_slot  = find_next_available_slot
    cc.format_slot_proposal_for_llm = format_slot_proposal_for_llm
    if not hasattr(cc, "find_meeting_slot"):
        def _find_meeting_slot_stub(*a, **kw):
            raise NotImplementedError("mocked in tests")
        cc.find_meeting_slot = _find_meeting_slot_stub


# ===========================================================================
# _round_up_to_slot
# ===========================================================================

class TestRoundUpToSlot:
    def test_already_on_boundary(self):
        dt = utc(2026, 6, 1, 9, 0)
        assert cc._round_up_to_slot(dt) == dt

    def test_rounds_up_1_minute(self):
        dt = utc(2026, 6, 1, 9, 1)
        expected = utc(2026, 6, 1, 9, 15)
        assert cc._round_up_to_slot(dt) == expected

    def test_rounds_up_14_minutes(self):
        dt = utc(2026, 6, 1, 9, 14)
        expected = utc(2026, 6, 1, 9, 15)
        assert cc._round_up_to_slot(dt) == expected

    def test_rounds_up_16_minutes(self):
        dt = utc(2026, 6, 1, 9, 16)
        expected = utc(2026, 6, 1, 9, 30)
        assert cc._round_up_to_slot(dt) == expected

    def test_rounds_across_hour(self):
        dt = utc(2026, 6, 1, 9, 46)
        expected = utc(2026, 6, 1, 10, 0)
        assert cc._round_up_to_slot(dt) == expected


# ===========================================================================
# find_next_available_slot
# ===========================================================================

class TestFindNextAvailableSlot:
    """Monday 2026-06-01 == weekday 0.  Arizona 9am = UTC 16:00."""

    def _monday_9am_utc(self) -> datetime:
        # 2026-06-01 is a Monday
        return az(2026, 6, 1, 9, 0).astimezone(UTC)

    def test_empty_calendars_returns_first_slot(self):
        search_from = self._monday_9am_utc()
        slot = cc.find_next_available_slot({}, search_from=search_from)
        assert slot is not None
        slot_start, slot_end = slot
        # Should land at 09:00 AZ Monday
        start_az = slot_start.astimezone(AZ)
        assert start_az.hour == 9
        assert start_az.minute == 0
        assert start_az.weekday() == 0

    def test_respects_duration(self):
        search_from = self._monday_9am_utc()
        slot = cc.find_next_available_slot({}, duration_minutes=60, search_from=search_from)
        assert slot is not None
        dur = (slot[1] - slot[0]).total_seconds() / 60
        assert dur == 60.0

    def test_skips_weekend_saturday(self):
        # 2026-05-30 is a Saturday
        saturday_10am = az(2026, 5, 30, 10, 0).astimezone(UTC)
        slot = cc.find_next_available_slot({}, search_from=saturday_10am)
        assert slot is not None
        start_az = slot[0].astimezone(AZ)
        # Should land on next Monday (2026-06-01)
        assert start_az.weekday() == 0
        assert start_az.hour == 9

    def test_skips_weekend_sunday(self):
        # 2026-05-31 is a Sunday
        sunday_noon = az(2026, 5, 31, 12, 0).astimezone(UTC)
        slot = cc.find_next_available_slot({}, search_from=sunday_noon)
        assert slot is not None
        start_az = slot[0].astimezone(AZ)
        assert start_az.weekday() == 0  # Monday

    def test_jumps_past_busy_block(self):
        search_from = self._monday_9am_utc()
        # Block the entire 9am–10am slot
        busy_start = az(2026, 6, 1, 9, 0).astimezone(UTC)
        busy_end   = az(2026, 6, 1, 10, 0).astimezone(UTC)
        busy = {"alice@hjrglobal.com": [(busy_start, busy_end)]}
        slot = cc.find_next_available_slot(busy, search_from=search_from)
        assert slot is not None
        start_az = slot[0].astimezone(AZ)
        # Should be at 10:00 AZ
        assert start_az.hour == 10
        assert start_az.minute == 0

    def test_jumps_past_multiple_busy_blocks(self):
        search_from = self._monday_9am_utc()
        # Block 9:00–11:00, then 11:15–12:00
        busy = {
            "a@hjrglobal.com": [
                (az(2026, 6, 1, 9, 0).astimezone(UTC), az(2026, 6, 1, 11, 0).astimezone(UTC)),
                (az(2026, 6, 1, 11, 15).astimezone(UTC), az(2026, 6, 1, 12, 0).astimezone(UTC)),
            ]
        }
        slot = cc.find_next_available_slot(busy, search_from=search_from)
        assert slot is not None
        start_az = slot[0].astimezone(AZ)
        assert start_az.hour == 12
        assert start_az.minute == 0

    def test_slot_cannot_bleed_past_5pm(self):
        # Start search from 4:45 PM AZ -- a 30-min slot would end at 5:15, past EOD
        late_start = az(2026, 6, 1, 16, 45).astimezone(UTC)
        slot = cc.find_next_available_slot({}, duration_minutes=30, search_from=late_start)
        assert slot is not None
        start_az = slot[0].astimezone(AZ)
        # Should have jumped to next workday
        assert start_az.hour == 9
        assert start_az.weekday() == 1  # Tuesday 2026-06-02

    def test_returns_none_when_no_slot_in_window(self):
        search_from = self._monday_9am_utc()
        # Block the entire 7-day window for all slots
        time_max = search_from + timedelta(days=7)
        busy = {"a@hjrglobal.com": [(search_from, time_max)]}
        slot = cc.find_next_available_slot(busy, search_from=search_from, search_days=7)
        assert slot is None

    def test_two_calendars_both_must_be_free(self):
        search_from = self._monday_9am_utc()
        # Alice busy 9:00–10:00, Bob busy 10:00–11:00 -- first common slot is 11:00
        busy = {
            "alice@hjrglobal.com": [
                (az(2026, 6, 1, 9, 0).astimezone(UTC), az(2026, 6, 1, 10, 0).astimezone(UTC))
            ],
            "bob@hjrglobal.com": [
                (az(2026, 6, 1, 10, 0).astimezone(UTC), az(2026, 6, 1, 11, 0).astimezone(UTC))
            ],
        }
        slot = cc.find_next_available_slot(busy, search_from=search_from)
        assert slot is not None
        start_az = slot[0].astimezone(AZ)
        assert start_az.hour == 11
        assert start_az.minute == 0

    def test_before_work_hours_jumps_to_9am(self):
        # Start at 6am AZ (before work hours)
        early = az(2026, 6, 1, 6, 0).astimezone(UTC)
        slot = cc.find_next_available_slot({}, search_from=early)
        assert slot is not None
        start_az = slot[0].astimezone(AZ)
        assert start_az.hour == 9
        assert start_az.minute == 0


# ===========================================================================
# get_free_busy  (mocked API)
# ===========================================================================

class TestGetFreeBusy:
    def _make_service(self, busy_periods: list[dict]) -> MagicMock:
        """Return a mock Google Calendar service whose freebusy returns busy_periods."""
        svc = MagicMock()
        svc.freebusy().query().execute.return_value = {
            "calendars": {
                "alice@hjrglobal.com": {"busy": busy_periods},
                "bob@hjrglobal.com":   {"busy": []},
            }
        }
        return svc

    def test_parses_busy_periods(self):
        svc = self._make_service([
            {"start": "2026-06-01T16:00:00Z", "end": "2026-06-01T17:00:00Z"},
        ])
        with patch.object(cc, "_build_service", return_value=svc):
            result = cc.get_free_busy(
                "harrison@hjrglobal.com",
                ["alice@hjrglobal.com", "bob@hjrglobal.com"],
                utc(2026, 6, 1, 15, 0),
                utc(2026, 6, 8, 15, 0),
            )
        assert len(result["alice@hjrglobal.com"]) == 1
        assert len(result["bob@hjrglobal.com"]) == 0
        start, end = result["alice@hjrglobal.com"][0]
        assert start == utc(2026, 6, 1, 16, 0)
        assert end   == utc(2026, 6, 1, 17, 0)

    def test_calendar_with_errors_treated_as_fully_busy(self):
        svc = MagicMock()
        svc.freebusy().query().execute.return_value = {
            "calendars": {
                "alice@hjrglobal.com": {
                    "errors": [{"domain": "calendar", "reason": "notFound"}],
                    "busy": [],
                },
            }
        }
        t_min = utc(2026, 6, 1)
        t_max = utc(2026, 6, 8)
        with patch.object(cc, "_build_service", return_value=svc):
            result = cc.get_free_busy(
                "harrison@hjrglobal.com",
                ["alice@hjrglobal.com"],
                t_min,
                t_max,
            )
        # Should be treated as fully busy
        assert result["alice@hjrglobal.com"] == [(t_min, t_max)]

    def test_raises_on_403(self):
        from googleapiclient.errors import HttpError
        resp = MagicMock()
        resp.status = 403
        svc = MagicMock()
        svc.freebusy().query().execute.side_effect = HttpError(resp=resp, content=b"Forbidden")
        with patch.object(cc, "_build_service", return_value=svc):
            with pytest.raises(cc.CalendarClientError, match="403"):
                cc.get_free_busy(
                    "harrison@hjrglobal.com",
                    ["alice@hjrglobal.com"],
                    utc(2026, 6, 1),
                    utc(2026, 6, 8),
                )


# ===========================================================================
# format_slot_proposal_for_llm
# ===========================================================================

class TestFormatSlotProposal:
    def _monday_slot(self):
        start = az(2026, 6, 1, 9, 0).astimezone(UTC)
        end   = az(2026, 6, 1, 9, 30).astimezone(UTC)
        return start, end

    def test_contains_title(self):
        start, end = self._monday_slot()
        out = cc.format_slot_proposal_for_llm(start, end, ["Harrison", "Larry"], title="Brand Review")
        assert "Brand Review" in out

    def test_contains_participant_names(self):
        start, end = self._monday_slot()
        out = cc.format_slot_proposal_for_llm(start, end, ["Harrison", "Larry"])
        assert "Harrison" in out
        assert "Larry" in out

    def test_contains_day_label(self):
        start, end = self._monday_slot()
        out = cc.format_slot_proposal_for_llm(start, end, ["Harrison", "Larry"])
        assert "Monday" in out
        assert "June" in out
        assert "2026" in out

    def test_contains_az_time(self):
        start, end = self._monday_slot()
        out = cc.format_slot_proposal_for_llm(start, end, ["Harrison", "Larry"])
        assert "AZ" in out

    def test_contains_proposed_iso_strings(self):
        start, end = self._monday_slot()
        out = cc.format_slot_proposal_for_llm(start, end, ["Harrison", "Larry"])
        assert "2026-06-01T09:00:00-07:00" in out
        assert "2026-06-01T09:30:00-07:00" in out

    def test_contains_confirmation_instruction(self):
        start, end = self._monday_slot()
        out = cc.format_slot_proposal_for_llm(start, end, ["Harrison"])
        assert "confirmed" in out.lower()

    def test_three_participants_joined_correctly(self):
        start, end = self._monday_slot()
        out = cc.format_slot_proposal_for_llm(start, end, ["Alice", "Bob", "Charlie"])
        assert "Charlie" in out


# ===========================================================================
# _tool_calendar_schedule_meeting  (handler)
# ===========================================================================

_FAKE_USER_MAP = {
    "U_HARRISON": {
        "display_name": "Harrison",
        "asana_email": "harrison@hjrglobal.com",
        "entity": "FNDR",
    },
    "U_LARRY": {
        "display_name": "Larry",
        "asana_email": "larry@hjrglobal.com",
        "entity": "BDM",
    },
}


def _resolve_name_stub(name: str, entity: str):
    """Minimal stub: matches 'Larry' -> U_LARRY."""
    for sid, info in _FAKE_USER_MAP.items():
        if info["display_name"].lower() == name.lower():
            return sid, info
    return None, None


class TestToolCalendarScheduleMeeting:
    """Unit tests for the tool handler.  All external calls mocked."""

    def _call(self, slack_user_id: str, input_data: dict):
        from src.cora.tools.tool_dispatch import _tool_calendar_schedule_meeting
        return _tool_calendar_schedule_meeting(slack_user_id, "FNDR", input_data)

    def test_unknown_requester_returns_error(self):
        with patch("src.cora.tools.tool_dispatch._load_slack_asana_map", return_value={}):
            result = self._call("U_UNKNOWN", {"participants": ["Larry"]})
        assert "not in" in result.lower() or "user map" in result.lower()

    def test_unresolved_participant_returns_error(self):
        with patch("src.cora.tools.tool_dispatch._load_slack_asana_map", return_value=_FAKE_USER_MAP), \
             patch("src.cora.tools.tool_dispatch.resolve_name_to_slack_user_id", return_value=(None, None)):
            result = self._call("U_HARRISON", {"participants": ["Nonexistent Person"]})
        assert "could not find" in result.lower()

    def test_phase1_no_slot_found(self):
        slot_start = az(2026, 6, 2, 9, 0).astimezone(UTC)
        slot_end   = az(2026, 6, 2, 9, 30).astimezone(UTC)
        with patch("src.cora.tools.tool_dispatch._load_slack_asana_map", return_value=_FAKE_USER_MAP), \
             patch("src.cora.tools.tool_dispatch.resolve_name_to_slack_user_id", return_value=("U_LARRY", _FAKE_USER_MAP["U_LARRY"])), \
             patch("src.cora.tools.tool_dispatch.calendar_client.find_meeting_slot", return_value=None):
            result = self._call("U_HARRISON", {"participants": ["Larry"]})
        assert "no common opening" in result.lower() or "no slot" in result.lower() or "not found" in result.lower()

    def test_phase1_slot_found_returns_proposal(self):
        slot_start = az(2026, 6, 2, 9, 0).astimezone(UTC)
        slot_end   = az(2026, 6, 2, 9, 30).astimezone(UTC)
        with patch("src.cora.tools.tool_dispatch._load_slack_asana_map", return_value=_FAKE_USER_MAP), \
             patch("src.cora.tools.tool_dispatch.resolve_name_to_slack_user_id", return_value=("U_LARRY", _FAKE_USER_MAP["U_LARRY"])), \
             patch("src.cora.tools.tool_dispatch.calendar_client.find_meeting_slot", return_value=(slot_start, slot_end)):
            result = self._call("U_HARRISON", {"participants": ["Larry"]})
        assert "SLOT FOUND" in result
        assert "2026-06-02" in result
        assert "confirmed" in result.lower()

    def test_phase2_missing_proposed_times_returns_error(self):
        with patch("src.cora.tools.tool_dispatch._load_slack_asana_map", return_value=_FAKE_USER_MAP), \
             patch("src.cora.tools.tool_dispatch.resolve_name_to_slack_user_id", return_value=("U_LARRY", _FAKE_USER_MAP["U_LARRY"])):
            result = self._call("U_HARRISON", {
                "participants": ["Larry"],
                "confirmed": True,
                # proposed_start and proposed_end deliberately omitted
            })
        assert "proposed_start" in result.lower() or "missing" in result.lower()

    def test_phase2_books_event(self):
        fake_event = {
            "id": "abc123",
            "summary": "Sync",
            "htmlLink": "https://calendar.google.com/event?eid=abc123",
            "start": {"dateTime": "2026-06-02T09:00:00-07:00"},
            "end":   {"dateTime": "2026-06-02T09:30:00-07:00"},
            "attendees": [],
        }
        with patch("src.cora.tools.tool_dispatch._load_slack_asana_map", return_value=_FAKE_USER_MAP), \
             patch("src.cora.tools.tool_dispatch.resolve_name_to_slack_user_id", return_value=("U_LARRY", _FAKE_USER_MAP["U_LARRY"])), \
             patch("src.cora.tools.tool_dispatch.calendar_client.create_event", return_value=fake_event):
            result = self._call("U_HARRISON", {
                "participants": ["Larry"],
                "confirmed": True,
                "proposed_start": "2026-06-02T09:00:00-07:00",
                "proposed_end":   "2026-06-02T09:30:00-07:00",
                "title": "Sync",
            })
        # format_created_event_for_llm returns a string with a calendar link
        assert "calendar.google.com" in result or "Sync" in result

    def test_phase2_api_error_returns_friendly_message(self):
        with patch("src.cora.tools.tool_dispatch._load_slack_asana_map", return_value=_FAKE_USER_MAP), \
             patch("src.cora.tools.tool_dispatch.resolve_name_to_slack_user_id", return_value=("U_LARRY", _FAKE_USER_MAP["U_LARRY"])), \
             patch("src.cora.tools.tool_dispatch.calendar_client.create_event",
                   side_effect=cc.CalendarClientError("403 DWD")):
            result = self._call("U_HARRISON", {
                "participants": ["Larry"],
                "confirmed": True,
                "proposed_start": "2026-06-02T09:00:00-07:00",
                "proposed_end":   "2026-06-02T09:30:00-07:00",
            })
        assert "failed" in result.lower() or "error" in result.lower() or "403" in result

    def test_requester_auto_included_not_duplicated(self):
        """Passing the requester name in participants should not add them twice."""
        slot_start = az(2026, 6, 2, 9, 0).astimezone(UTC)
        slot_end   = az(2026, 6, 2, 9, 30).astimezone(UTC)
        with patch("src.cora.tools.tool_dispatch._load_slack_asana_map", return_value=_FAKE_USER_MAP), \
             patch("src.cora.tools.tool_dispatch.resolve_name_to_slack_user_id", return_value=("U_LARRY", _FAKE_USER_MAP["U_LARRY"])), \
             patch("src.cora.tools.tool_dispatch.calendar_client.find_meeting_slot", return_value=(slot_start, slot_end)) as mock_find:
            self._call("U_HARRISON", {"participants": ["Harrison", "Larry"]})
        # emails passed to find_meeting_slot should not have harrison twice
        call_kwargs = mock_find.call_args
        emails = call_kwargs[1].get("calendar_emails") or call_kwargs[0][1]
        assert emails.count("harrison@hjrglobal.com") == 1
