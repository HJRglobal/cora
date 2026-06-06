"""Tests for the shared nudge ledger (Fix 2, 2026-06-06)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import nudge_ledger as nl


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
