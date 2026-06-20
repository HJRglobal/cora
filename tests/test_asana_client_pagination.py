"""Regression tests: get_user_tasks pagination + Asana's limit<=100 cap.

The 2026-05-31 reconciliation "scale increase" passed max_tasks=200 straight
through as limit=200 -- Asana 400s on limit > 100, so every per-user fetch
failed silently until 2026-06-11. These tests pin the clamp + offset paging.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.tools import asana_client  # noqa: E402


def _resp(data, next_offset=None):
    r = MagicMock()
    r.status_code = 200
    body = {"data": data}
    if next_offset:
        body["next_page"] = {"offset": next_offset}
    r.json.return_value = body
    return r


def _client_returning(responses):
    """Mock httpx.Client context manager yielding queued responses per .get()."""
    client = MagicMock()
    client.get.side_effect = responses
    cm = MagicMock()
    cm.__enter__.return_value = client
    cm.__exit__.return_value = False
    return client, MagicMock(return_value=cm)


@pytest.fixture(autouse=True)
def _pat_env(monkeypatch):
    monkeypatch.setenv("ASANA_PAT", "test-pat")


class TestLimitClamp:
    def test_limit_never_exceeds_100(self):
        tasks_page = [{"gid": str(i)} for i in range(100)]
        client, factory = _client_returning([_resp(tasks_page)])
        with patch.object(asana_client.httpx, "Client", factory):
            asana_client.get_user_tasks("u1", max_tasks=200)
        sent = client.get.call_args.kwargs["params"]
        assert sent["limit"] == 100

    def test_small_max_tasks_passes_through(self):
        client, factory = _client_returning([_resp([{"gid": "1"}])])
        with patch.object(asana_client.httpx, "Client", factory):
            asana_client.get_user_tasks("u1", max_tasks=25)
        sent = client.get.call_args.kwargs["params"]
        assert sent["limit"] == 25
        assert "offset" not in sent


class TestPagination:
    def test_paginates_to_max_tasks(self):
        page1 = [{"gid": str(i)} for i in range(100)]
        page2 = [{"gid": str(100 + i)} for i in range(50)]
        client, factory = _client_returning([
            _resp(page1, next_offset="tok123"),
            _resp(page2),  # no next_page -> stop
        ])
        with patch.object(asana_client.httpx, "Client", factory):
            tasks = asana_client.get_user_tasks("u1", max_tasks=200)
        assert len(tasks) == 150
        first_params = client.get.call_args_list[0].kwargs["params"]
        second_params = client.get.call_args_list[1].kwargs["params"]
        assert "offset" not in first_params
        assert second_params["offset"] == "tok123"
        assert second_params["limit"] == 100

    def test_stops_at_max_tasks(self):
        page1 = [{"gid": str(i)} for i in range(100)]
        page2 = [{"gid": str(100 + i)} for i in range(100)]
        client, factory = _client_returning([
            _resp(page1, next_offset="tok1"),
            _resp(page2, next_offset="tok2"),  # offset present but cap reached
        ])
        with patch.object(asana_client.httpx, "Client", factory):
            tasks = asana_client.get_user_tasks("u1", max_tasks=150)
        assert len(tasks) == 150
        assert client.get.call_count == 2
        assert client.get.call_args_list[1].kwargs["params"]["limit"] == 50

    def test_single_page_no_next_stops(self):
        client, factory = _client_returning([_resp([{"gid": "1"}, {"gid": "2"}])])
        with patch.object(asana_client.httpx, "Client", factory):
            tasks = asana_client.get_user_tasks("u1", max_tasks=200)
        assert len(tasks) == 2
        assert client.get.call_count == 1


class TestSystemNoiseFilteredAtSource:
    """WS12: goal-reminder system tasks are dropped at the get_user_tasks source."""

    def test_goal_reminder_dropped(self):
        page = [
            {"gid": "1", "name": "Ship the deck"},
            {"gid": "2", "name": "It's time to update your goal"},
            {"gid": "3", "name": "Call the vendor"},
        ]
        client, factory = _client_returning([_resp(page)])
        with patch.object(asana_client.httpx, "Client", factory):
            tasks = asana_client.get_user_tasks("u1", max_tasks=50)
        gids = [t["gid"] for t in tasks]
        assert gids == ["1", "3"]  # system reminder removed

    def test_real_tasks_all_kept(self):
        page = [{"gid": "1", "name": "A"}, {"gid": "2", "name": "B"}]
        client, factory = _client_returning([_resp(page)])
        with patch.object(asana_client.httpx, "Client", factory):
            tasks = asana_client.get_user_tasks("u1", max_tasks=50)
        assert len(tasks) == 2


class TestOptFieldsThreading:
    """WS12: opt_fields param defaults to the rich set; a narrow list is honored."""

    def test_default_opt_fields_include_notes_and_projects(self):
        client, factory = _client_returning([_resp([{"gid": "1"}])])
        with patch.object(asana_client.httpx, "Client", factory):
            asana_client.get_user_tasks("u1", max_tasks=10)
        sent = client.get.call_args.kwargs["params"]["opt_fields"]
        assert "notes" in sent and "projects.name" in sent

    def test_narrow_opt_fields_passed_through(self):
        narrow = ["name", "permalink_url", "assignee.gid"]
        client, factory = _client_returning([_resp([{"gid": "1"}])])
        with patch.object(asana_client.httpx, "Client", factory):
            asana_client.get_user_tasks("u1", max_tasks=10, opt_fields=narrow)
        sent = client.get.call_args.kwargs["params"]["opt_fields"]
        assert sent == "name,permalink_url,assignee.gid"
        assert "notes" not in sent and "memberships" not in sent


class TestErrorsStillRaise:
    def test_400_raises(self):
        r = MagicMock()
        r.status_code = 400
        r.text = "limit: Value must be <= 100"
        client, factory = _client_returning([r])
        with patch.object(asana_client.httpx, "Client", factory):
            with pytest.raises(asana_client.AsanaClientError, match="400"):
                asana_client.get_user_tasks("u1", max_tasks=50)
