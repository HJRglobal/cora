"""Tests for the shared nudge ledger (Fix 2, 2026-06-06).

Closed-task guard tests added 2026-06-11 (Hannah report: nudges firing on
already-closed tasks -- no nudge source re-checked completion at fire time).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import nudge_ledger as nl
import cora.tools.asana_client as asana_client
from cora.tools.asana_client import AsanaClientError


def _write_log(path, rows):
    lines = [json.dumps({"_schema": "closure-nudges throttle log"})]
    lines += [json.dumps(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestRecentlyNudged:
    def test_missing_file_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(tmp_path / "nope.jsonl"))
        assert nl.recently_nudged("123") is False

    def test_recent_row_returns_true(self, tmp_path, monkeypatch):
        p = tmp_path / "log.jsonl"
        recent = datetime.now(timezone.utc).isoformat()
        _write_log(p, [{"task_gid": "123", "last_nudged_at": recent, "nudge_count": 1}])
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        assert nl.recently_nudged("123", within_days=14) is True

    def test_old_row_returns_false(self, tmp_path, monkeypatch):
        p = tmp_path / "log.jsonl"
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        _write_log(p, [{"task_gid": "123", "last_nudged_at": old, "nudge_count": 1}])
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        assert nl.recently_nudged("123", within_days=14) is False

    def test_other_task_returns_false(self, tmp_path, monkeypatch):
        p = tmp_path / "log.jsonl"
        recent = datetime.now(timezone.utc).isoformat()
        _write_log(p, [{"task_gid": "999", "last_nudged_at": recent, "nudge_count": 1}])
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        assert nl.recently_nudged("123", within_days=14) is False

    def test_schema_header_skipped(self, tmp_path, monkeypatch):
        p = tmp_path / "log.jsonl"
        _write_log(p, [])  # header only
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        assert nl.recently_nudged("123") is False

    def test_empty_gid_returns_false(self):
        assert nl.recently_nudged("") is False


class TestRecordNudge:
    def test_appends_row(self, tmp_path, monkeypatch):
        p = tmp_path / "log.jsonl"
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        assert nl.record_nudge("123", task_name="Do thing", assignee_user="Matt") is True
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        assert rows[0]["task_gid"] == "123"
        assert rows[0]["nudge_count"] == 1
        assert rows[0]["task_name"] == "Do thing"

    def test_increments_count(self, tmp_path, monkeypatch):
        p = tmp_path / "log.jsonl"
        _write_log(p, [{"task_gid": "123", "last_nudged_at": "2026-01-01T00:00:00+00:00", "nudge_count": 2}])
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        nl.record_nudge("123", task_name="again")
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip() and "_schema" not in l]
        assert rows[-1]["nudge_count"] == 3

    def test_record_then_recently_nudged_true(self, tmp_path, monkeypatch):
        p = tmp_path / "log.jsonl"
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        nl.record_nudge("123", task_name="x")
        assert nl.recently_nudged("123", within_days=7) is True

    def test_empty_gid_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(tmp_path / "log.jsonl"))
        assert nl.record_nudge("") is False


def _rows(path):
    return [
        json.loads(l)
        for l in path.read_text().splitlines()
        if l.strip() and "_schema" not in l
    ]


def _completed(completed_at):
    return {"completed": True, "completed_at": completed_at}


class TestPermanentlyExcluded:
    def test_permanent_closed_row_excludes(self, tmp_path, monkeypatch):
        p = tmp_path / "log.jsonl"
        _write_log(p, [{
            "task_gid": "123", "reason": "already_closed", "permanent": True,
            "last_nudged_at": "2026-01-01T00:00:00+00:00",
        }])
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        assert nl.permanently_excluded("123") is True

    def test_non_permanent_closed_row_does_not_exclude(self, tmp_path, monkeypatch):
        p = tmp_path / "log.jsonl"
        _write_log(p, [{
            "task_gid": "123", "reason": "already_closed", "permanent": False,
            "last_nudged_at": "2026-01-01T00:00:00+00:00",
        }])
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        assert nl.permanently_excluded("123") is False

    def test_plain_nudge_row_does_not_exclude(self, tmp_path, monkeypatch):
        p = tmp_path / "log.jsonl"
        _write_log(p, [{"task_gid": "123", "last_nudged_at": "2026-01-01T00:00:00+00:00", "nudge_count": 1}])
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        assert nl.permanently_excluded("123") is False

    def test_missing_file_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(tmp_path / "nope.jsonl"))
        assert nl.permanently_excluded("123") is False


class TestClosedTaskGuard:
    def test_completed_long_ago_skips_and_records_permanent(self, tmp_path, monkeypatch):
        p = tmp_path / "log.jsonl"
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        with patch.object(asana_client, "get_task_completion", return_value=_completed(old)):
            assert nl.closed_task_guard("123", task_name="Old closed task") is True
        rows = _rows(p)
        assert len(rows) == 1
        assert rows[0]["reason"] == "already_closed"
        assert rows[0]["permanent"] is True
        assert rows[0]["task_gid"] == "123"

    def test_completed_recently_skips_with_non_permanent_row(self, tmp_path, monkeypatch):
        p = tmp_path / "log.jsonl"
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        fresh = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        with patch.object(asana_client, "get_task_completion", return_value=_completed(fresh)):
            assert nl.closed_task_guard("123") is True
        rows = _rows(p)
        assert rows[0]["reason"] == "already_closed"
        assert rows[0]["permanent"] is False

    def test_completed_missing_timestamp_is_permanent(self, tmp_path, monkeypatch):
        p = tmp_path / "log.jsonl"
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        with patch.object(asana_client, "get_task_completion", return_value=_completed(None)):
            assert nl.closed_task_guard("123") is True
        assert _rows(p)[0]["permanent"] is True

    def test_open_task_does_not_skip_or_record(self, tmp_path, monkeypatch):
        p = tmp_path / "log.jsonl"
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        with patch.object(
            asana_client, "get_task_completion",
            return_value={"completed": False, "completed_at": None},
        ):
            assert nl.closed_task_guard("123") is False
        assert not p.exists()

    def test_fetch_error_fails_open(self, tmp_path, monkeypatch):
        p = tmp_path / "log.jsonl"
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        with patch.object(
            asana_client, "get_task_completion",
            side_effect=AsanaClientError("boom"),
        ):
            assert nl.closed_task_guard("123") is False
        assert not p.exists()

    def test_permanent_exclusion_short_circuits_api(self, tmp_path, monkeypatch):
        """A recorded permanent exclusion never re-fetches from Asana."""
        p = tmp_path / "log.jsonl"
        _write_log(p, [{
            "task_gid": "123", "reason": "already_closed", "permanent": True,
            "last_nudged_at": "2026-01-01T00:00:00+00:00",
        }])
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        with patch.object(
            asana_client, "get_task_completion",
            side_effect=AssertionError("must not be called"),
        ):
            assert nl.closed_task_guard("123") is True

    def test_skip_row_throttles_other_system(self, tmp_path, monkeypatch):
        """The skip row carries last_nudged_at so recently_nudged() (the weekly
        sweep's lockout field) also suppresses the task."""
        p = tmp_path / "log.jsonl"
        monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(p))
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        with patch.object(asana_client, "get_task_completion", return_value=_completed(old)):
            nl.closed_task_guard("123")
        assert nl.recently_nudged("123", within_days=14) is True

    def test_empty_gid_returns_false(self):
        assert nl.closed_task_guard("") is False
