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
    assert result["nudges_sent"] == 2  # 2 users, 1 task each => 2 nudges (dry run counts)


def test_run_posts_comment_when_not_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(nudges, "THROTTLE_FILE", tmp_path / "throttle.json")
    monkeypatch.setattr(nudges, "KB_DB_FILE", tmp_path / "cora_kb.db")
    task = _make_task("222", "Follow up Allen Flavors", _overdue_date(20))

    with patch.object(nudges, "_load_users", return_value=[_make_users()[0]]), \
         patch("run_asana_hygiene_nudges.get_user_tasks", return_value=[task]), \
         patch("run_asana_hygiene_nudges.create_task_comment") as mock_comment:
        result = nudges.run(dry_run=False)

    mock_comment.assert_called_once()
    assert result["nudges_sent"] == 1


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
         patch("run_asana_hygiene_nudges.create_task_comment"):
        result = nudges.run(dry_run=False)

    assert result["nudges_sent"] <= nudges.MAX_TOTAL
