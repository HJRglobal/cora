"""Tests for run_asana_hygiene_nudges.py -- Feature #14."""

from __future__ import annotations

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import run_asana_hygiene_nudges as nudges  # noqa: E402


# ---------------------------------------------------------------------------
# _is_visibility_task
# ---------------------------------------------------------------------------

def test_visibility_task_by_name():
    assert nudges._is_visibility_task("Review Visibility report") is True


def test_visibility_task_andrew_stubbs():
    assert nudges._is_visibility_task("Call Andrew Stubbs re: taxes") is True


def test_visibility_task_hayden():
    assert nudges._is_visibility_task("Send docs to Hayden Greber") is True


def test_visibility_task_not_matched():
    assert nudges._is_visibility_task("Review OSN reconciliation") is False


def test_visibility_task_emily_stubbs():
    assert nudges._is_visibility_task("Follow up Emily Stubbs re APA") is True


# ---------------------------------------------------------------------------
# _is_lex_task
# ---------------------------------------------------------------------------

def test_lex_task_detected():
    task = {
        "memberships": [
            {"project": {"name": "[LEX-LLC] Operations"}, "section": {"name": "Active"}}
        ]
    }
    assert nudges._is_lex_task(task) is True


def test_lex_task_not_detected():
    task = {
        "memberships": [
            {"project": {"name": "[F3E] Sales"}, "section": {"name": "Active"}}
        ]
    }
    assert nudges._is_lex_task(task) is False


def test_lex_task_empty_memberships():
    assert nudges._is_lex_task({"memberships": []}) is False


# ---------------------------------------------------------------------------
# _days_overdue
# ---------------------------------------------------------------------------

def test_days_overdue_past():
    past = (date.today() - timedelta(days=20)).isoformat()
    assert nudges._days_overdue(past) == 20


def test_days_overdue_future():
    future = (date.today() + timedelta(days=5)).isoformat()
    assert nudges._days_overdue(future) < 0


def test_days_overdue_invalid():
    assert nudges._days_overdue("not-a-date") == 0


def test_days_overdue_today():
    assert nudges._days_overdue(date.today().isoformat()) == 0


# ---------------------------------------------------------------------------
# _build_comment
# ---------------------------------------------------------------------------

def test_build_comment_contains_days():
    comment = nudges._build_comment("Tommy", 25, "12345")
    assert "25 days" in comment


def test_build_comment_contains_today():
    comment = nudges._build_comment("Hannah", 30, "99999")
    assert date.today().isoformat() in comment


def test_build_comment_contains_cora():
    comment = nudges._build_comment("Harrison", 15, "11111")
    assert "Cora" in comment


# ---------------------------------------------------------------------------
# _load_throttle / _save_throttle
# ---------------------------------------------------------------------------

def test_load_throttle_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "nonexistent.json")
    assert nudges._load_throttle() == {}


def test_save_and_load_throttle(tmp_path, monkeypatch):
    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "throttle.json")
    state = {"task_gid_1": 1234567890}
    nudges._save_throttle(state)
    loaded = nudges._load_throttle()
    assert loaded == state


# ---------------------------------------------------------------------------
# _has_kb_signal -- against a temp DB matching the real knowledge_chunks schema
# ---------------------------------------------------------------------------

def _make_kb_db(path, rows):
    """rows: list of (content, date_modified_epoch)."""
    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE knowledge_chunks ("
        "chunk_id TEXT, source TEXT, content TEXT, "
        "date_modified INTEGER, ingested_at INTEGER)"
    )
    for i, (content, dm) in enumerate(rows):
        conn.execute(
            "INSERT INTO knowledge_chunks (chunk_id, content, date_modified, ingested_at) "
            "VALUES (?, ?, ?, ?)",
            (str(i), content, dm, int(time.time())),  # ingested_at always 'now'
        )
    conn.commit()
    conn.close()


def test_kb_signal_missing_db_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(nudges, "KB_DB_FILE", tmp_path / "nope.db")
    assert nudges._has_kb_signal("Follow up Allen Flavors") is False


def test_kb_signal_recent_match_returns_true(tmp_path, monkeypatch):
    db = tmp_path / "kb.db"
    recent = int(time.time()) - 3 * 86400  # 3 days ago (within 30d window)
    _make_kb_db(db, [("Notes about Follow up Allen Flavors deadline", recent)])
    monkeypatch.setattr(nudges, "KB_DB_FILE", db)
    assert nudges._has_kb_signal("Follow up Allen Flavors") is True


def test_kb_signal_old_match_returns_false(tmp_path, monkeypatch):
    """A matching chunk whose source was modified long ago is NOT recent signal."""
    db = tmp_path / "kb.db"
    old = int(time.time()) - 90 * 86400  # 90 days ago (outside 30d window)
    _make_kb_db(db, [("Follow up Allen Flavors", old)])
    monkeypatch.setattr(nudges, "KB_DB_FILE", db)
    assert nudges._has_kb_signal("Follow up Allen Flavors") is False


def test_kb_signal_no_content_match_returns_false(tmp_path, monkeypatch):
    db = tmp_path / "kb.db"
    recent = int(time.time()) - 1 * 86400
    _make_kb_db(db, [("Completely unrelated content", recent)])
    monkeypatch.setattr(nudges, "KB_DB_FILE", db)
    assert nudges._has_kb_signal("Follow up Allen Flavors") is False


def test_kb_signal_uses_date_modified_not_ingested_at(tmp_path, monkeypatch):
    """Regression: recency must key on date_modified, not ingested_at.

    A chunk freshly ingested (ingested_at=now) but with an OLD date_modified must
    NOT count as recent activity -- otherwise a full KB re-ingest would make every
    task look 'recently active' and suppress all nudges.
    """
    db = tmp_path / "kb.db"
    old = int(time.time()) - 200 * 86400  # source last touched 200d ago
    _make_kb_db(db, [("Follow up Allen Flavors", old)])  # ingested_at = now inside helper
    monkeypatch.setattr(nudges, "KB_DB_FILE", db)
    assert nudges._has_kb_signal("Follow up Allen Flavors") is False


# ---------------------------------------------------------------------------
# run() -- with mocked dependencies
# ---------------------------------------------------------------------------

def _make_task(gid: str, name: str, due_on: str, memberships: list | None = None) -> dict:
    return {
        "gid": gid,
        "name": name,
        "due_on": due_on,
        "memberships": memberships or [],
        "permalink_url": f"https://app.asana.com/0/0/{gid}",
    }


def _overdue_date(days: int = 20) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def _make_users() -> list[dict]:
    return [
        {
            "slack_user_id": "U0B2RM2JYJ1",
            "asana_user_gid": "1204525779609669",
            "display_name": "Harrison Rogers",
        },
        {
            "slack_user_id": "U0B3AEQS0NB",
            "asana_user_gid": "1209060959783860",
            "display_name": "Hannah Grant",
        },
    ]


def test_run_dry_run_no_comment_posted(tmp_path, monkeypatch):
    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "throttle.json")
    monkeypatch.setattr(nudges, "KB_DB_FILE", tmp_path / "cora_kb.db")
    task = _make_task("111", "Review OSN AR", _overdue_date(20))

    with patch.object(nudges, "_load_users", return_value=_make_users()), \
         patch("run_asana_hygiene_nudges.get_user_tasks", return_value=[task]), \
         patch("run_asana_hygiene_nudges.create_task_comment") as mock_comment:
        result = nudges.run(dry_run=True)

    mock_comment.assert_not_called()
    # In-memory throttle deduplicates same task GID across users in one run,
    # so only the first user's task fires. At least 1 nudge must be counted.
    assert result["nudges_sent"] >= 1


def test_run_posts_comment_when_not_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "throttle.json")
    monkeypatch.setattr(nudges, "KB_DB_FILE", tmp_path / "cora_kb.db")
    task = _make_task("222", "Follow up Allen Flavors", _overdue_date(20))

    with patch.object(nudges, "_load_users", return_value=[_make_users()[0]]), \
         patch("run_asana_hygiene_nudges.get_user_tasks", return_value=[task]), \
         patch("run_asana_hygiene_nudges.closed_task_guard", return_value=False), \
         patch("run_asana_hygiene_nudges.create_task_comment") as mock_comment:
        result = nudges.run(dry_run=False)

    mock_comment.assert_called_once()
    assert result["nudges_sent"] == 1


def test_run_skips_task_nudged_by_other_system(tmp_path, monkeypatch):
    """Cross-system lockout: a task already nudged in the shared closure JSONL
    (e.g. by the weekly Cowork sweep) within 14d is skipped by the daily job."""
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    ledger = tmp_path / "closure-nudges-throttle.jsonl"
    recent = _dt.now(_tz.utc).isoformat()
    ledger.write_text(
        _json.dumps({"_schema": "x"}) + "\n"
        + _json.dumps({"task_gid": "333", "last_nudged_at": recent, "nudge_count": 1}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(ledger))
    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "throttle.json")
    monkeypatch.setattr(nudges, "KB_DB_FILE", tmp_path / "cora_kb.db")
    task = _make_task("333", "Already nudged elsewhere", _overdue_date(20))

    with patch.object(nudges, "_load_users", return_value=[_make_users()[0]]), \
         patch("run_asana_hygiene_nudges.get_user_tasks", return_value=[task]), \
         patch("run_asana_hygiene_nudges.create_task_comment") as mock_comment:
        result = nudges.run(dry_run=False)

    mock_comment.assert_not_called()
    assert result["nudges_sent"] == 0


def test_run_records_nudge_to_shared_ledger(tmp_path, monkeypatch):
    """A fired nudge is appended to the shared ledger so the weekly sweep sees it."""
    import json as _json

    ledger = tmp_path / "closure-nudges-throttle.jsonl"
    monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(ledger))
    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "throttle.json")
    monkeypatch.setattr(nudges, "KB_DB_FILE", tmp_path / "cora_kb.db")
    task = _make_task("444", "Fresh stale task", _overdue_date(20))

    with patch.object(nudges, "_load_users", return_value=[_make_users()[0]]), \
         patch("run_asana_hygiene_nudges.get_user_tasks", return_value=[task]), \
         patch("run_asana_hygiene_nudges.closed_task_guard", return_value=False), \
         patch("run_asana_hygiene_nudges.create_task_comment") as mock_comment:
        result = nudges.run(dry_run=False)

    mock_comment.assert_called_once()
    assert result["nudges_sent"] == 1
    assert ledger.exists()
    rows = [_json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
    assert any(r.get("task_gid") == "444" for r in rows)


def test_run_skips_visibility_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "throttle.json")
    monkeypatch.setattr(nudges, "KB_DB_FILE", tmp_path / "cora_kb.db")
    task = _make_task("333", "Send docs to Hayden Greber", _overdue_date(20))

    with patch.object(nudges, "_load_users", return_value=[_make_users()[0]]), \
         patch("run_asana_hygiene_nudges.get_user_tasks", return_value=[task]), \
         patch("run_asana_hygiene_nudges.create_task_comment") as mock_comment:
        result = nudges.run(dry_run=False)

    mock_comment.assert_not_called()
    assert result["nudges_sent"] == 0


def test_run_respects_throttle(tmp_path, monkeypatch):
    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "throttle.json")
    monkeypatch.setattr(nudges, "KB_DB_FILE", tmp_path / "cora_kb.db")
    task = _make_task("444", "OSN recon review", _overdue_date(20))

    # Pre-set throttle for this task
    nudges._save_throttle({"444": int(time.time()) - 86400})  # 1 day ago < 7-day window

    with patch.object(nudges, "_load_users", return_value=[_make_users()[0]]), \
         patch("run_asana_hygiene_nudges.get_user_tasks", return_value=[task]), \
         patch("run_asana_hygiene_nudges.create_task_comment") as mock_comment:
        result = nudges.run(dry_run=False)

    mock_comment.assert_not_called()
    assert result["skipped_throttle"] == 1


def test_run_skips_tasks_not_overdue_enough(tmp_path, monkeypatch):
    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "throttle.json")
    monkeypatch.setattr(nudges, "KB_DB_FILE", tmp_path / "cora_kb.db")
    task = _make_task("555", "Check in with Larry", _overdue_date(5))  # only 5 days overdue

    with patch.object(nudges, "_load_users", return_value=[_make_users()[0]]), \
         patch("run_asana_hygiene_nudges.get_user_tasks", return_value=[task]), \
         patch("run_asana_hygiene_nudges.create_task_comment") as mock_comment:
        result = nudges.run(dry_run=False)

    mock_comment.assert_not_called()
    assert result["nudges_sent"] == 0


def test_run_respects_max_per_user(tmp_path, monkeypatch):
    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "throttle.json")
    monkeypatch.setattr(nudges, "KB_DB_FILE", tmp_path / "cora_kb.db")
    # 10 overdue tasks for 1 user -- should only nudge MAX_PER_USER
    tasks = [_make_task(str(i), f"Task {i}", _overdue_date(20)) for i in range(10)]

    with patch.object(nudges, "_load_users", return_value=[_make_users()[0]]), \
         patch("run_asana_hygiene_nudges.get_user_tasks", return_value=tasks), \
         patch("run_asana_hygiene_nudges.closed_task_guard", return_value=False), \
         patch("run_asana_hygiene_nudges.create_task_comment"):
        result = nudges.run(dry_run=False)

    assert result["nudges_sent"] <= nudges.MAX_PER_USER


def test_run_respects_max_total(tmp_path, monkeypatch):
    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "throttle.json")
    monkeypatch.setattr(nudges, "KB_DB_FILE", tmp_path / "cora_kb.db")
    # 6 users each with 10 tasks -- total should not exceed MAX_TOTAL
    many_users = [
        {"asana_user_gid": str(i), "display_name": f"User {i}"}
        for i in range(6)
    ]
    tasks = [_make_task(f"{uid}_{i}", f"Task {uid} {i}", _overdue_date(20)) for uid in range(6) for i in range(10)]

    def mock_get_tasks(gid, max_tasks=25):
        return [_make_task(f"{gid}_{i}", f"Task {gid} {i}", _overdue_date(20)) for i in range(10)]

    with patch.object(nudges, "_load_users", return_value=many_users), \
         patch("run_asana_hygiene_nudges.get_user_tasks", side_effect=mock_get_tasks), \
         patch("run_asana_hygiene_nudges.closed_task_guard", return_value=False), \
         patch("run_asana_hygiene_nudges.create_task_comment"):
        result = nudges.run(dry_run=False)

    assert result["nudges_sent"] <= nudges.MAX_TOTAL


# ---------------------------------------------------------------------------
# Fire-time closed-task guard (2026-06-11 -- Hannah report: nudges were firing
# on tasks already closed; no source re-checked completion at fire time)
# ---------------------------------------------------------------------------

def test_run_skips_task_closed_at_fire_time(tmp_path, monkeypatch):
    """Closed task -> no comment posted, counted as skipped_closed."""
    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "throttle.json")
    monkeypatch.setattr(nudges, "KB_DB_FILE", tmp_path / "cora_kb.db")
    task = _make_task("666", "Closed while queued", _overdue_date(20))

    with patch.object(nudges, "_load_users", return_value=[_make_users()[0]]), \
         patch("run_asana_hygiene_nudges.get_user_tasks", return_value=[task]), \
         patch("run_asana_hygiene_nudges.closed_task_guard", return_value=True) as mock_guard, \
         patch("run_asana_hygiene_nudges.create_task_comment") as mock_comment:
        result = nudges.run(dry_run=False)

    mock_guard.assert_called_once()
    mock_comment.assert_not_called()
    assert result["nudges_sent"] == 0
    assert result["skipped_closed"] == 1


def test_run_closed_task_skip_recorded_in_ledger(tmp_path, monkeypatch):
    """End-to-end through the REAL guard: a task whose live Asana state is
    completed gets no comment and a reason=already_closed ledger row."""
    import json as _json
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    import cora.tools.asana_client as _ac

    ledger = tmp_path / "closure-nudges-throttle.jsonl"
    monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(ledger))
    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "throttle.json")
    monkeypatch.setattr(nudges, "KB_DB_FILE", tmp_path / "cora_kb.db")
    task = _make_task("777", "Closed long ago", _overdue_date(60))
    closed_at = (_dt.now(_tz.utc) - _td(days=9)).isoformat()

    with patch.object(nudges, "_load_users", return_value=[_make_users()[0]]), \
         patch("run_asana_hygiene_nudges.get_user_tasks", return_value=[task]), \
         patch.object(_ac, "get_task_completion",
                      return_value={"completed": True, "completed_at": closed_at}), \
         patch("run_asana_hygiene_nudges.create_task_comment") as mock_comment:
        result = nudges.run(dry_run=False)

    mock_comment.assert_not_called()
    assert result["skipped_closed"] == 1
    rows = [_json.loads(l) for l in ledger.read_text().splitlines() if l.strip()]
    closed_rows = [r for r in rows if r.get("reason") == "already_closed"]
    assert len(closed_rows) == 1
    assert closed_rows[0]["task_gid"] == "777"
    assert closed_rows[0]["permanent"] is True


def test_run_open_task_unaffected_by_guard(tmp_path, monkeypatch):
    """Open task -> the real guard passes it through and the nudge fires."""
    import cora.tools.asana_client as _ac

    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "throttle.json")
    monkeypatch.setattr(nudges, "KB_DB_FILE", tmp_path / "cora_kb.db")
    task = _make_task("888", "Still open and stale", _overdue_date(20))

    with patch.object(nudges, "_load_users", return_value=[_make_users()[0]]), \
         patch("run_asana_hygiene_nudges.get_user_tasks", return_value=[task]), \
         patch.object(_ac, "get_task_completion",
                      return_value={"completed": False, "completed_at": None}), \
         patch("run_asana_hygiene_nudges.create_task_comment") as mock_comment:
        result = nudges.run(dry_run=False)

    mock_comment.assert_called_once()
    assert result["nudges_sent"] == 1
    assert result["skipped_closed"] == 0


def test_run_permanent_exclusion_skips_without_api_call(tmp_path, monkeypatch):
    """A previously recorded permanent already_closed row skips the task with
    no Asana fetch at all."""
    import json as _json

    import cora.tools.asana_client as _ac

    ledger = tmp_path / "closure-nudges-throttle.jsonl"
    ledger.write_text(
        _json.dumps({
            "task_gid": "999", "reason": "already_closed", "permanent": True,
            "last_nudged_at": "2026-01-01T00:00:00+00:00",
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLOSURE_NUDGE_LOG_PATH", str(ledger))
    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "throttle.json")
    monkeypatch.setattr(nudges, "KB_DB_FILE", tmp_path / "cora_kb.db")
    task = _make_task("999", "Permanently excluded", _overdue_date(120))

    with patch.object(nudges, "_load_users", return_value=[_make_users()[0]]), \
         patch("run_asana_hygiene_nudges.get_user_tasks", return_value=[task]), \
         patch.object(_ac, "get_task_completion",
                      side_effect=AssertionError("must not be called")), \
         patch("run_asana_hygiene_nudges.create_task_comment") as mock_comment:
        result = nudges.run(dry_run=False)

    mock_comment.assert_not_called()
    assert result["skipped_closed"] == 1
