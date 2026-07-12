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
# calendar_client.delete_event() unit tests (F-06, 2026-07-12)
# ---------------------------------------------------------------------------

class TestDeleteEvent:
    def _svc(self, side_effect=None):
        svc = MagicMock()
        req = MagicMock()
        if side_effect is not None:
            req.execute.side_effect = side_effect
        svc.events.return_value.delete.return_value = req
        return svc

    def test_happy_path_calls_delete(self):
        svc = self._svc()
        with patch("cora.tools.calendar_client._build_service", return_value=svc):
            cal.delete_event(user_email="h@example.com", event_id="evt123")
        svc.events.return_value.delete.assert_called_once()

    def test_missing_event_id_raises(self):
        with pytest.raises(cal.CalendarClientError, match="event_id"):
            cal.delete_event(user_email="h@example.com", event_id="")

    def test_410_gone_is_idempotent_success(self):
        from googleapiclient.errors import HttpError
        resp = MagicMock()
        resp.status = 410
        svc = self._svc(side_effect=HttpError(resp, b"gone"))
        with patch("cora.tools.calendar_client._build_service", return_value=svc):
            cal.delete_event(user_email="h@example.com", event_id="evt123")  # no raise

    def test_404_not_found_is_idempotent_success(self):
        from googleapiclient.errors import HttpError
        resp = MagicMock()
        resp.status = 404
        svc = self._svc(side_effect=HttpError(resp, b"not found"))
        with patch("cora.tools.calendar_client._build_service", return_value=svc):
            cal.delete_event(user_email="h@example.com", event_id="evt123")  # no raise


# ---------------------------------------------------------------------------
# tool_dispatch calendar write tools -- server-side pending store (F-05 / F-06)
# ---------------------------------------------------------------------------

_MOCK_MAP = {
    "U_HARRISON": {"display_name": "Harrison", "asana_email": "harrison@hjrglobal.com"},
}
_CH = "cora-build"


@pytest.fixture(autouse=True)
def _clear_calendar_pending():
    td._PENDING_CALENDAR_WRITES.clear()
    yield
    td._PENDING_CALENDAR_WRITES.clear()


class TestToolCalendarCreateEventStaged:
    """F-05: the honor-system confirmed flag is replaced by a server-side pending
    store -- the first call always previews, only the confirm turn books, and it
    books from the STASH (never the confirm-turn fields)."""

    def _call(self, input_data: dict, user_id="U_HARRISON"):
        input_data = {**input_data, "_channel_name": _CH}
        with patch("cora.tools.tool_dispatch._load_slack_asana_map", return_value=_MOCK_MAP):
            return td._tool_calendar_create_event(user_id, "FNDR", input_data)

    def test_first_call_previews_and_does_not_create(self):
        with patch("cora.tools.calendar_client.create_event") as mock_create:
            result = self._call({"summary": "Hold", "start": "2026-07-13T09:00",
                                 "end": "2026-07-13T09:30"})
        assert "NOT CREATED" in result
        mock_create.assert_not_called()
        assert td.has_pending_calendar_write("U_HARRISON", _CH)

    def test_first_call_confirmed_true_re_previews_never_books(self):
        # A first-call confirmed=true must NOT book -- there is no stash yet.
        with patch("cora.tools.calendar_client.create_event") as mock_create:
            result = self._call({"summary": "Hold", "start": "2026-07-13T09:00",
                                 "end": "2026-07-13T09:30", "confirmed": True})
        assert "NOT CREATED" in result
        mock_create.assert_not_called()

    def test_two_call_flow_books_from_stash(self):
        mock_event = _mock_created_event(summary="Hold")
        with patch("cora.tools.calendar_client.create_event", return_value=mock_event) as mock_create:
            self._call({"summary": "Hold", "start": "2026-07-13T09:00", "end": "2026-07-13T09:30"})
            result = self._call({"confirmed": True})  # bare confirm -- no fields
        assert "CREATED" in result
        mock_create.assert_called_once()
        assert mock_create.call_args.kwargs["summary"] == "Hold"

    def test_confirm_uses_stash_not_confirm_turn_fields(self):
        # THE F-05 invariant: the model paraphrasing different fields on the confirm
        # turn cannot change what gets booked -- the stash is authoritative.
        mock_event = _mock_created_event()
        with patch("cora.tools.calendar_client.create_event", return_value=mock_event) as mock_create:
            self._call({"summary": "Real Hold", "start": "2026-07-13T09:00", "end": "2026-07-13T09:30"})
            self._call({"summary": "DIFFERENT", "start": "2030-01-01T00:00",
                       "end": "2030-01-01T01:00", "confirmed": True})
        assert mock_create.call_args.kwargs["summary"] == "Real Hold"
        assert mock_create.call_args.kwargs["start"] == "2026-07-13T09:00"

    def test_missing_summary_on_preview_returns_message(self):
        result = self._call({"start": "2026-07-13T09:00", "end": "2026-07-13T09:30"})
        assert "summary" in result.lower()

    def test_unknown_user_returns_message(self):
        with patch("cora.tools.tool_dispatch._load_slack_asana_map", return_value={}):
            result = td._tool_calendar_create_event(
                "U_UNKNOWN", "FNDR",
                {"summary": "X", "start": "2026-07-13T09:00", "end": "2026-07-13T09:30",
                 "_channel_name": _CH},
            )
        assert "not mapped" in result.lower()

    def test_book_error_returns_friendly_message(self):
        with patch("cora.tools.calendar_client.create_event",
                   side_effect=cal.CalendarClientError("403 DWD scope missing")):
            self._call({"summary": "Hold", "start": "2026-07-13T09:00", "end": "2026-07-13T09:30"})
            result = self._call({"confirmed": True})
        assert "error" in result.lower() and "wasn't created" in result.lower()

    def test_attendees_string_stashed_and_booked_as_list(self):
        mock_event = _mock_created_event()
        with patch("cora.tools.calendar_client.create_event", return_value=mock_event) as mock_create:
            self._call({"summary": "Sync", "start": "2026-07-13T09:00", "end": "2026-07-13T09:30",
                       "attendees": "alex@hjrglobal.com,hannah@hjrglobal.com"})
            self._call({"confirmed": True})
        assert mock_create.call_args.kwargs["attendees"] == [
            "alex@hjrglobal.com", "hannah@hjrglobal.com"]


class TestToolCalendarDeleteEventStaged:
    """F-06: confirm-gated delete on the shared pending store."""

    def _call(self, input_data: dict, user_id="U_HARRISON"):
        input_data = {**input_data, "_channel_name": _CH}
        with patch("cora.tools.tool_dispatch._load_slack_asana_map", return_value=_MOCK_MAP):
            return td._tool_calendar_delete_event(user_id, "FNDR", input_data)

    def test_no_query_asks(self):
        result = self._call({})
        assert "which event" in result.lower() or "won't guess" in result.lower()

    def test_query_single_match_previews_not_deletes(self):
        events = [{"id": "evt1", "summary": "SMOKE TEST HOLD"}]
        with patch("cora.tools.calendar_client.get_user_events", return_value=(events, "this week")), \
             patch("cora.tools.calendar_client.delete_event") as mock_del:
            result = self._call({"query": "smoke test"})
        assert "NOT CANCELLED" in result
        mock_del.assert_not_called()
        assert td.has_pending_calendar_write("U_HARRISON", _CH)

    def test_query_multi_match_asks_which(self):
        events = [{"id": "e1", "summary": "Sync A"}, {"id": "e2", "summary": "Sync B"}]
        with patch("cora.tools.calendar_client.get_user_events", return_value=(events, "this week")):
            result = self._call({"query": "sync"})
        assert "which one" in result.lower()

    def test_query_no_match_refuses(self):
        with patch("cora.tools.calendar_client.get_user_events", return_value=([], "this week")):
            result = self._call({"query": "nonexistent"})
        assert "couldn't find" in result.lower()

    def test_two_call_flow_deletes_stashed_id(self):
        events = [{"id": "evt1", "summary": "SMOKE TEST HOLD"}]
        with patch("cora.tools.calendar_client.get_user_events", return_value=(events, "this week")), \
             patch("cora.tools.calendar_client.delete_event") as mock_del:
            self._call({"query": "smoke test"})
            result = self._call({"confirmed": True})
        assert "Cancelled" in result
        assert mock_del.call_args.kwargs["event_id"] == "evt1"

    def test_confirm_without_pending_re_prompts_never_deletes(self):
        with patch("cora.tools.calendar_client.delete_event") as mock_del:
            result = self._call({"confirmed": True})  # no prior preview
        mock_del.assert_not_called()
        assert "which event" in result.lower() or "won't guess" in result.lower()
