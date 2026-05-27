"""Tests for LEX Cora tools -- lex_revalidation_status and lex_staff_pulse.

Tests are structured as three layers:
  Layer A: pure unit tests (no network, all mocked)
  Layer B: integration-style tests on the format output (mock at Asana boundary)

Run: python -m pytest tests/test_lex_tools.py -v
"""

import re
from datetime import date, datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.cora.tools import lex_client
from src.cora.tools.tool_dispatch import (
    _tool_lex_revalidation_status,
    _tool_lex_staff_pulse,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_task(
    gid="1215070649606664",
    name="AZ DDD Therapy Revalidation",
    completed=False,
    permalink_url="https://app.asana.com/0/123/456",
):
    return {
        "gid": gid,
        "name": name,
        "completed": completed,
        "permalink_url": permalink_url,
        "due_on": "2026-06-30",
        "assignee": {"name": "Harrison Rogers"},
        "modified_at": "2026-05-24T22:00:00Z",
    }


def _make_subtask(name, completed=False, due_on="2026-06-15", assignee_name="Justin Gilmore"):
    return {
        "gid": "999",
        "name": name,
        "completed": completed,
        "due_on": due_on,
        "assignee": {"name": assignee_name},
        "permalink_url": f"https://app.asana.com/0/1/{name[:10]}",
    }


def _make_story(author="Harrison Rogers", days_ago=3):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    return {
        "type": "comment",
        "created_at": ts,
        "created_by": {"name": author},
        "text": "Sent revalidation documents to tguzman@azdes.gov",
    }


# ---------------------------------------------------------------------------
# Layer A: unit tests on helper functions
# ---------------------------------------------------------------------------


class TestDaysRemaining:
    """Verify deadline calculations based on today's date."""

    def test_days_remaining_positive(self, monkeypatch):
        monkeypatch.setattr(lex_client, "_REVALIDATION_DEADLINE", date(2026, 6, 30))
        # Patch date.today() via freezing date
        with patch("src.cora.tools.lex_client.date") as mock_date:
            mock_date.today.return_value = date(2026, 5, 26)
            mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
            days = (lex_client._REVALIDATION_DEADLINE - date(2026, 5, 26)).days
        assert days == 35

    def test_deadline_is_june_30(self):
        assert lex_client._REVALIDATION_DEADLINE == date(2026, 6, 30)

    def test_task_gid_constant(self):
        assert lex_client._REVALIDATION_TASK_GID == "1215070649606664"


class TestGetTaskError:
    """Verify error handling in _get_task."""

    def test_404_raises(self):
        resp = MagicMock()
        resp.status_code = 404
        resp.text = "Not Found"
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = resp
            with pytest.raises(lex_client.LexClientError, match="not found"):
                lex_client._get_task("bad_gid")

    def test_401_raises(self):
        resp = MagicMock()
        resp.status_code = 401
        resp.text = "Unauthorized"
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = resp
            with pytest.raises(lex_client.LexClientError, match="401"):
                lex_client._get_task("123")

    def test_missing_pat_raises(self, monkeypatch):
        monkeypatch.delenv("ASANA_PAT", raising=False)
        with pytest.raises(lex_client.LexClientError, match="ASANA_PAT"):
            lex_client._pat()


class TestGetSubtasks:
    """Verify subtask fetching returns empty list on error (non-fatal)."""

    def test_non_200_returns_empty(self):
        resp = MagicMock()
        resp.status_code = 500
        resp.json.return_value = {}
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = resp
            result = lex_client._get_subtasks("123")
        assert result == []


class TestGetLatestStory:
    """Verify story filtering selects only comment-type entries."""

    def test_filters_to_comments_only(self):
        stories = [
            {"type": "system", "created_at": "2026-05-01T10:00:00Z", "created_by": {"name": "Asana"}, "text": "task created"},
            {"type": "comment", "created_at": "2026-05-20T10:00:00Z", "created_by": {"name": "Harrison Rogers"}, "text": "Updated"},
        ]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": stories}
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = resp
            result = lex_client._get_latest_story("123")
        assert result["type"] == "comment"
        assert result["created_by"]["name"] == "Harrison Rogers"

    def test_returns_none_if_no_comments(self):
        stories = [
            {"type": "system", "created_at": "2026-05-01T10:00:00Z", "created_by": {"name": "Asana"}, "text": "task created"},
        ]
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"data": stories}
        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = resp
            result = lex_client._get_latest_story("123")
        assert result is None


# ---------------------------------------------------------------------------
# Layer B: integration-style tests on get_revalidation_status output
# ---------------------------------------------------------------------------


class TestGetRevalidationStatusOutput:
    """Verify formatted output contains expected sections."""

    def _call_with_mocks(self, task, subtasks, story, today=None):
        with (
            patch.object(lex_client, "_get_task", return_value=task),
            patch.object(lex_client, "_get_subtasks", return_value=subtasks),
            patch.object(lex_client, "_get_latest_story", return_value=story),
            patch("src.cora.tools.lex_client.date") as mock_date,
        ):
            if today:
                mock_date.today.return_value = today
                mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
            result = lex_client.get_revalidation_status()
        return result

    def test_contains_task_name(self):
        result = self._call_with_mocks(
            _make_task(),
            [_make_subtask("Submit Form A"), _make_subtask("Submit Form B", completed=True)],
            _make_story(),
            today=date(2026, 5, 26),
        )
        assert "AZ DDD Therapy Revalidation" in result

    def test_days_remaining_in_output(self):
        result = self._call_with_mocks(
            _make_task(),
            [],
            None,
            today=date(2026, 5, 26),
        )
        # 2026-06-30 - 2026-05-26 = 35 days
        assert "35d" in result

    def test_open_blockers_listed(self):
        subtasks = [
            _make_subtask("File AHCCCS Form", completed=False),
            _make_subtask("Send supporting docs", completed=False),
            _make_subtask("Confirm receipt", completed=True),
        ]
        result = self._call_with_mocks(_make_task(), subtasks, None, today=date(2026, 5, 26))
        assert "Open blockers" in result
        assert "File AHCCCS Form" in result
        assert "2 of 3" in result or "Completed" in result

    def test_completed_task_shows_complete(self):
        result = self._call_with_mocks(
            _make_task(completed=True),
            [],
            _make_story(),
            today=date(2026, 6, 15),
        )
        assert "COMPLETE" in result

    def test_critical_marker_when_7_days_or_less(self):
        result = self._call_with_mocks(
            _make_task(completed=False),
            [],
            None,
            today=date(2026, 6, 25),
        )
        assert "CRITICAL" in result

    def test_last_comment_age_shown(self):
        result = self._call_with_mocks(
            _make_task(),
            [],
            _make_story(author="Shaun Hawkins", days_ago=5),
            today=date(2026, 5, 26),
        )
        assert "Last comment" in result
        assert "Shaun Hawkins" in result

    def test_no_comments_shows_none_on_record(self):
        result = self._call_with_mocks(
            _make_task(),
            [],
            None,
            today=date(2026, 5, 26),
        )
        assert "none on record" in result

    def test_asana_link_present(self):
        result = self._call_with_mocks(
            _make_task(permalink_url="https://app.asana.com/0/123/456"),
            [],
            None,
            today=date(2026, 5, 26),
        )
        assert "https://app.asana.com/0/123/456" in result

    def test_returns_unknown_response_on_task_fetch_error(self):
        with (
            patch.object(lex_client, "_get_task", side_effect=lex_client.LexClientError("network error")),
        ):
            result = lex_client.get_revalidation_status()
        assert "I don't have that right now" in result

    def test_passed_deadline_shows_urgent(self):
        result = self._call_with_mocks(
            _make_task(completed=False),
            [],
            None,
            today=date(2026, 7, 5),
        )
        assert "DEADLINE PASSED" in result or "URGENT" in result

    def test_no_subtasks_shows_fallback(self):
        result = self._call_with_mocks(
            _make_task(),
            [],
            None,
            today=date(2026, 5, 26),
        )
        assert "No sub-task" in result


# ---------------------------------------------------------------------------
# Layer B: staff_pulse stub output
# ---------------------------------------------------------------------------


class TestGetStaffPulse:
    """Verify staff pulse returns blocked stub with clear explanation."""

    def test_returns_stub_message(self):
        result = lex_client.get_staff_pulse()
        assert "pipeline" in result.lower() or "not yet available" in result.lower()
        assert "Sean" in result or "Drive" in result

    def test_does_not_raise(self):
        """Tool should never raise -- it's a stub."""
        result = lex_client.get_staff_pulse()
        assert isinstance(result, str)
        assert len(result) > 10


# ---------------------------------------------------------------------------
# Layer B: dispatch layer smoke tests
# ---------------------------------------------------------------------------


class TestDispatchLayer:
    """Smoke tests for tool_dispatch wiring."""

    def test_lex_revalidation_dispatches(self):
        with (
            patch.object(lex_client, "_get_task", return_value=_make_task()),
            patch.object(lex_client, "_get_subtasks", return_value=[]),
            patch.object(lex_client, "_get_latest_story", return_value=None),
            patch("src.cora.tools.lex_client.date") as mock_date,
        ):
            mock_date.today.return_value = date(2026, 5, 26)
            mock_date.side_effect = lambda *args, **kw: date(*args, **kw)
            result = _tool_lex_revalidation_status("U_TEST", "LEX", {})
        assert "AZ DDD Therapy Revalidation" in result

    def test_lex_staff_pulse_dispatches(self):
        result = _tool_lex_staff_pulse("U_TEST", "LEX", {})
        assert isinstance(result, str)
        assert len(result) > 10

    def test_revalidation_in_tool_definitions(self):
        from src.cora.tools.tool_dispatch import TOOL_DEFINITIONS
        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert "lex_revalidation_status" in names

    def test_staff_pulse_in_tool_definitions(self):
        from src.cora.tools.tool_dispatch import TOOL_DEFINITIONS
        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert "lex_staff_pulse" in names

    def test_revalidation_in_tool_functions(self):
        from src.cora.tools.tool_dispatch import _TOOL_FUNCTIONS
        assert "lex_revalidation_status" in _TOOL_FUNCTIONS

    def test_staff_pulse_in_tool_functions(self):
        from src.cora.tools.tool_dispatch import _TOOL_FUNCTIONS
        assert "lex_staff_pulse" in _TOOL_FUNCTIONS
