"""Unit tests for calendar_client.create_event() and tool_dispatch calendar_create_event."""

from unittest.mock import MagicMock, patch

import pytest

import cora.tools.calendar_client as cal
import cora.tools.tool_dispatch as td


# ---------------------------------------------------------------------------
# calendar_client.create_event() unit tests
# ---------------------------------------------------------------------------

def _mock_created_event(event_id="evt123", summary="Test Meeting", html_link="https://cal.google.com/event?eid=abc"):
    return {
        "id": event_id,
        "summary": summary,
        "htmlLink": html_link,
        "start": {"dateTime": "2026-05-25T14:00:00-07:00", "timeZone": "America/Phoenix"},
        "end": {"dateTime": "2026-05-25T15:00:00-07:00", "timeZone": "America/Phoenix"},
        "attendees": [],
    }


def _build_mock_service(insert_return=None, insert_side_effect=None):
    """Build a mock Calendar service whose events().insert().execute() is pre-wired."""
    svc = MagicMock()
    req = MagicMock()
    if insert_side_effect is not None:
        req.execute.side_effect = insert_side_effect
    else:
        req.execute.return_value = insert_return or _mock_created_event()
    svc.events.return_value.insert.return_value = req
    return svc


class TestParseDateTime:
    def test_naive_iso_gets_phoenix_offset(self):
        result = cal._parse_datetime_input("2026-05-25T14:00")
        assert "-07:00" in result
        assert "2026-05-25T14:00:00" in result

    def test_aware_iso_returned_as_is(self):
        result = cal._parse_datetime_input("2026-05-25T14:00:00-07:00")
        assert result == "2026-05-25T14:00:00-07:00"

    def test_space_separator_normalised(self):
        result = cal._parse_datetime_input("2026-05-25 14:00")
        assert "2026-05-25T14:00" in result

    def test_garbage_raises(self):
        with pytest.raises(cal.CalendarClientError, match="Cannot parse"):
            cal._parse_datetime_input("not-a-date")


class TestCreateEvent:
    def test_happy_path_returns_event(self):
        mock_svc = _build_mock_service(insert_return=_mock_created_event())
        with patch("cora.tools.calendar_client._build_service", return_value=mock_svc):
            event = cal.create_event(
                user_email="harrison@hjrglobal.com",
                summary="Test Meeting",
                start="2026-05-25T14:00",
                end="2026-05-25T15:00",
            )
        assert event["id"] == "evt123"
        mock_svc.events.return_value.insert.assert_called_once()

    def test_missing_summary_raises(self):
        with pytest.raises(cal.CalendarClientError, match="summary"):
            cal.create_event(
                user_email="h@example.com",
                summary="",
                start="2026-05-25T14:00",
                end="2026-05-25T15:00",
            )

    def test_end_before_start_raises(self):
        with pytest.raises(cal.CalendarClientError, match="after start"):
            cal.create_event(
                user_email="h@example.com",
                summary="Bad Event",
                start="2026-05-25T15:00",
                end="2026-05-25T14:00",
            )

    def test_invalid_attendee_email_raises(self):
        with pytest.raises(cal.CalendarClientError, match="email"):
            cal.create_event(
                user_email="h@example.com",
                summary="Meeting",
                start="2026-05-25T14:00",
                end="2026-05-25T15:00",
                attendees=["not-an-email"],
            )

    def test_attendees_included_in_body(self):
        mock_svc = _build_mock_service()
        with patch("cora.tools.calendar_client._build_service", return_value=mock_svc):
            cal.create_event(
                user_email="h@example.com",
                summary="Meeting",
                start="2026-05-25T14:00",
                end="2026-05-25T15:00",
                attendees=["alex@hjrglobal.com", "hannah@hjrglobal.com"],
            )
        call_kwargs = mock_svc.events.return_value.insert.call_args
        body = call_kwargs.kwargs.get("body") or call_kwargs[1].get("body") or call_kwargs[0][1]
        assert len(body["attendees"]) == 2

    def test_description_and_location_included(self):
        mock_svc = _build_mock_service()
        with patch("cora.tools.calendar_client._build_service", return_value=mock_svc):
            cal.create_event(
                user_email="h@example.com",
                summary="Meeting",
                start="2026-05-25T14:00",
                end="2026-05-25T15:00",
                description="Discuss Q2",
                location="HJR Office",
            )
        call_kwargs = mock_svc.events.return_value.insert.call_args
        body = call_kwargs.kwargs.get("body") or call_kwargs[1].get("body") or call_kwargs[0][1]
        assert body["description"] == "Discuss Q2"
        assert body["location"] == "HJR Office"

    def test_missing_sa_path_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)
            with pytest.raises(cal.CalendarClientError, match="GOOGLE_SERVICE_ACCOUNT_JSON"):
                cal.create_event(
                    user_email="h@example.com",
                    summary="Meeting",
                    start="2026-05-25T14:00",
                    end="2026-05-25T15:00",
                )


class TestFormatCreatedEvent:
    def test_contains_title_and_link(self):
        event = _mock_created_event(summary="Board Meeting", html_link="https://cal.google.com/x")
        result = cal.format_created_event_for_llm(event, user_email="harrison@hjrglobal.com")
        assert "Board Meeting" in result
        assert "<https://cal.google.com/x|Open in Google Calendar>" in result
        assert "harrison@hjrglobal.com" in result

    def test_handles_missing_link_gracefully(self):
        event = _mock_created_event(html_link="")
        result = cal.format_created_event_for_llm(event, user_email="h@example.com")
        assert "no calendar link" in result

    def test_attendees_shown_when_present(self):
        event = _mock_created_event()
        event["attendees"] = [{"email": "alex@hjrglobal.com"}]
        result = cal.format_created_event_for_llm(event, user_email="h@example.com")
        assert "alex@hjrglobal.com" in result


# ---------------------------------------------------------------------------
# tool_dispatch._tool_calendar_create_event() integration tests
# ---------------------------------------------------------------------------

_MOCK_MAP = {
    "U_HARRISON": {"display_name": "Harrison", "asana_email": "harrison@hjrglobal.com"},
}


class TestToolCalendarCreateEvent:
    def _call(self, input_data: dict, user_id="U_HARRISON"):
        with patch("cora.tools.tool_dispatch._load_slack_asana_map", return_value=_MOCK_MAP):
            return td._tool_calendar_create_event(user_id, "FNDR", input_data)

    def test_refuses_without_confirmed(self):
        result = self._call({"summary": "Meeting", "start": "2026-05-25T14:00", "end": "2026-05-25T15:00"})
        assert "refused" in result.lower()

    def test_refuses_with_confirmed_false(self):
        result = self._call({"summary": "Meeting", "start": "2026-05-25T14:00", "end": "2026-05-25T15:00", "confirmed": False})
        assert "refused" in result.lower()

    def test_missing_summary_returns_message(self):
        result = self._call({"start": "2026-05-25T14:00", "end": "2026-05-25T15:00", "confirmed": True})
        assert "summary" in result.lower()

    def test_missing_start_returns_message(self):
        result = self._call({"summary": "Meeting", "end": "2026-05-25T15:00", "confirmed": True})
        assert "start" in result.lower()

    def test_unknown_user_returns_message(self):
        with patch("cora.tools.tool_dispatch._load_slack_asana_map", return_value={}):
            result = td._tool_calendar_create_event(
                "U_UNKNOWN", "FNDR",
                {"summary": "X", "start": "2026-05-25T14:00", "end": "2026-05-25T15:00", "confirmed": True},
            )
        assert "not mapped" in result.lower()

    def test_successful_creation_returns_confirmation(self):
        mock_event = _mock_created_event(summary="Board Meeting")
        with patch("cora.tools.tool_dispatch._load_slack_asana_map", return_value=_MOCK_MAP), \
             patch("cora.tools.calendar_client.create_event", return_value=mock_event):
            result = td._tool_calendar_create_event(
                "U_HARRISON", "FNDR",
                {
                    "summary": "Board Meeting",
                    "start": "2026-05-25T14:00",
                    "end": "2026-05-25T15:00",
                    "confirmed": True,
                },
            )
        assert "Board Meeting" in result
        assert "CREATED" in result

    def test_calendar_error_returns_friendly_message(self):
        with patch("cora.tools.tool_dispatch._load_slack_asana_map", return_value=_MOCK_MAP), \
             patch("cora.tools.calendar_client.create_event",
                   side_effect=cal.CalendarClientError("403 DWD scope missing")):
            result = td._tool_calendar_create_event(
                "U_HARRISON", "FNDR",
                {
                    "summary": "Meeting",
                    "start": "2026-05-25T14:00",
                    "end": "2026-05-25T15:00",
                    "confirmed": True,
                },
            )
        assert "error" in result.lower()
        assert "wasn't created" in result.lower()

    def test_attendees_as_string_parsed(self):
        mock_event = _mock_created_event()
        with patch("cora.tools.tool_dispatch._load_slack_asana_map", return_value=_MOCK_MAP), \
             patch("cora.tools.calendar_client.create_event", return_value=mock_event) as mock_create:
            td._tool_calendar_create_event(
                "U_HARRISON", "FNDR",
                {
                    "summary": "Team Sync",
                    "start": "2026-05-25T14:00",
                    "end": "2026-05-25T15:00",
                    "attendees": "alex@hjrglobal.com,hannah@hjrglobal.com",
                    "confirmed": True,
                },
            )
        call_kwargs = mock_create.call_args
        attendees_passed = call_kwargs.kwargs.get("attendees") or call_kwargs[1].get("attendees")
        assert attendees_passed == ["alex@hjrglobal.com", "hannah@hjrglobal.com"]
