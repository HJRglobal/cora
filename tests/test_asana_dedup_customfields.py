"""Tests for asana_client dedup + custom-field helpers (added 2026-06-06).

Covers:
  - find_recent_duplicate_task: exact open match within window -> gid;
    completed / too-old / no-match -> None; fail-open on error.
  - set_task_custom_fields: 200 -> True; 4xx -> False (never raises).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import cora.tools.asana_client as ac


def _ctx_client(get_side_effect=None, put_response=None):
    """Build a mock httpx.Client usable as a context manager."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    if get_side_effect is not None:
        client.get.side_effect = get_side_effect
    if put_response is not None:
        client.put.return_value = put_response
    return client


def _resp(status=200, json_data=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data or {}
    r.text = ""
    return r


_ENV = {"ASANA_PAT": "test-pat"}
_RECENT = datetime.now(timezone.utc).isoformat()
_OLD = "2023-01-01T00:00:00+00:00"


class TestFindRecentDuplicateTask:
    def test_empty_name_returns_none(self):
        assert ac.find_recent_duplicate_task("") is None

    def test_open_recent_exact_match_returns_gid(self):
        typeahead = _resp(json_data={"data": [{"gid": "111", "name": "Send proposal"}]})
        task = _resp(json_data={"data": {"gid": "111", "completed": False, "created_at": _RECENT, "name": "Send proposal"}})
        client = _ctx_client(get_side_effect=[typeahead, task])
        with patch.dict("os.environ", _ENV), patch("httpx.Client", return_value=client):
            assert ac.find_recent_duplicate_task("Send proposal") == "111"

    def test_completed_match_returns_none(self):
        typeahead = _resp(json_data={"data": [{"gid": "111", "name": "Send proposal"}]})
        task = _resp(json_data={"data": {"gid": "111", "completed": True, "created_at": _RECENT}})
        client = _ctx_client(get_side_effect=[typeahead, task])
        with patch.dict("os.environ", _ENV), patch("httpx.Client", return_value=client):
            assert ac.find_recent_duplicate_task("Send proposal") is None

    def test_too_old_match_returns_none(self):
        typeahead = _resp(json_data={"data": [{"gid": "111", "name": "Send proposal"}]})
        task = _resp(json_data={"data": {"gid": "111", "completed": False, "created_at": _OLD}})
        client = _ctx_client(get_side_effect=[typeahead, task])
        with patch.dict("os.environ", _ENV), patch("httpx.Client", return_value=client):
            assert ac.find_recent_duplicate_task("Send proposal") is None

    def test_name_mismatch_returns_none(self):
        typeahead = _resp(json_data={"data": [{"gid": "111", "name": "Different task"}]})
        client = _ctx_client(get_side_effect=[typeahead])
        with patch.dict("os.environ", _ENV), patch("httpx.Client", return_value=client):
            assert ac.find_recent_duplicate_task("Send proposal") is None

    def test_case_insensitive_match(self):
        typeahead = _resp(json_data={"data": [{"gid": "111", "name": "SEND Proposal"}]})
        task = _resp(json_data={"data": {"gid": "111", "completed": False, "created_at": _RECENT}})
        client = _ctx_client(get_side_effect=[typeahead, task])
        with patch.dict("os.environ", _ENV), patch("httpx.Client", return_value=client):
            assert ac.find_recent_duplicate_task("send proposal") == "111"

    def test_typeahead_error_fails_open(self):
        client = _ctx_client(get_side_effect=[_resp(status=500)])
        with patch.dict("os.environ", _ENV), patch("httpx.Client", return_value=client):
            assert ac.find_recent_duplicate_task("Send proposal") is None


class TestSetTaskCustomFields:
    def test_success_returns_true(self):
        client = _ctx_client(put_response=_resp(status=200))
        with patch.dict("os.environ", _ENV), patch("httpx.Client", return_value=client):
            assert ac.set_task_custom_fields("999", {"fgid": "ogid"}) is True

    def test_bad_request_returns_false_no_raise(self):
        client = _ctx_client(put_response=_resp(status=400))
        with patch.dict("os.environ", _ENV), patch("httpx.Client", return_value=client):
            assert ac.set_task_custom_fields("999", {"fgid": "ogid"}) is False

    def test_empty_fields_returns_false(self):
        assert ac.set_task_custom_fields("999", {}) is False
