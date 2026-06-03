"""Tests for Feature #6: Asana Due-Date DM Escalation."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest


# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

def _import_module():
    import importlib.util, sys
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "run_due_date_escalation",
        Path(__file__).resolve().parents[1] / "scripts" / "run_due_date_escalation.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_due_date_escalation"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _import_module()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _az_now():
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-7)))


def _make_task(gid="T001", name="Do something", due_on=None, url="https://app.asana.com/0/1/T001"):
    return {"gid": gid, "name": name, "due_on": due_on, "permalink_url": url}


def _make_user(slack_id="U001", asana_gid="G001", name="Test User"):
    return {"slack_user_id": slack_id, "asana_user_gid": asana_gid, "display_name": name}


def _make_slack():
    slack = MagicMock()
    slack.conversations_open.return_value = {"channel": {"id": "DM001"}}
    slack.chat_postMessage.return_value = {"ok": True}
    return slack


# ---------------------------------------------------------------------------
# _is_due_soon tests
# ---------------------------------------------------------------------------

class TestIsDueSoon:
    def test_today_is_due_soon(self):
        now = _az_now()
        today_str = now.strftime("%Y-%m-%d")
        assert mod._is_due_soon(today_str, now) is True

    def test_tomorrow_is_due_soon(self):
        now = _az_now()
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        assert mod._is_due_soon(tomorrow_str, now) is True

    def test_yesterday_not_due_soon(self):
        now = _az_now()
        yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        assert mod._is_due_soon(yesterday_str, now) is False

    def test_two_days_out_not_due_soon(self):
        now = _az_now()
        future_str = (now + timedelta(days=2)).strftime("%Y-%m-%d")
        assert mod._is_due_soon(future_str, now) is False

    def test_none_due_on_returns_false(self):
        assert mod._is_due_soon(None, _az_now()) is False

    def test_empty_string_returns_false(self):
        assert mod._is_due_soon("", _az_now()) is False

    def test_invalid_format_returns_false(self):
        assert mod._is_due_soon("not-a-date", _az_now()) is False


# ---------------------------------------------------------------------------
# _is_throttled tests
# ---------------------------------------------------------------------------

class TestThrottle:
    def test_new_key_not_throttled(self):
        assert mod._is_throttled({}, "key1", 3600) is False

    def test_recent_key_throttled(self):
        throttle = {"key1": time.time() - 100}
        assert mod._is_throttled(throttle, "key1", 3600) is True

    def test_expired_key_not_throttled(self):
        throttle = {"key1": time.time() - 7200}
        assert mod._is_throttled(throttle, "key1", 3600) is False

    def test_task_throttle_48h(self):
        throttle = {"task:T001": time.time() - 86400}  # 24h ago
        # 48h window -> not expired
        assert mod._is_throttled(throttle, "task:T001", mod._TASK_THROTTLE_SECONDS) is True

    def test_decision_throttle_7d(self):
        throttle = {"decision:abc": time.time() - (6 * 86400)}  # 6 days ago
        assert mod._is_throttled(throttle, "decision:abc", mod._DECISION_THROTTLE_SECONDS) is True


# ---------------------------------------------------------------------------
# run_pass1_due_tasks tests
# ---------------------------------------------------------------------------

class TestPass1:
    def _run(self, tasks, throttle=None, dry_run=False, due_on=None):
        slack = _make_slack()
        now = _az_now()
        today = now.strftime("%Y-%m-%d")
        due = due_on or today
        user_tasks = [_make_task(due_on=due, gid=f"T{i}") for i, _ in enumerate(tasks)]
        users = [_make_user(slack_id="U001", asana_gid="G001")]
        t = throttle or {}

        with patch("cora.tools.asana_client.get_user_tasks", return_value=user_tasks):
            stats = mod.run_pass1_due_tasks(slack, users, t, now, dry_run)
        return slack, stats, t

    def test_due_today_sends_dm(self):
        slack, stats, _ = self._run(["task1"])
        assert stats["alerted"] == 1
        slack.chat_postMessage.assert_called_once()

    def test_not_due_soon_no_dm(self):
        future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
        slack, stats, _ = self._run(["task1"], due_on=future)
        assert stats["alerted"] == 0
        slack.chat_postMessage.assert_not_called()

    def test_throttled_task_not_re_sent(self):
        throttle = {"task:T0": time.time() - 100}  # recent
        slack, stats, _ = self._run(["task1"], throttle=throttle)
        assert stats["throttled"] == 1
        slack.chat_postMessage.assert_not_called()

    def test_throttle_key_set_after_alert(self):
        slack, stats, throttle = self._run(["task1"])
        assert "task:T0" in throttle

    def test_dry_run_no_dm_sent(self):
        slack, stats, _ = self._run(["task1"], dry_run=True)
        assert stats["alerted"] == 1
        slack.chat_postMessage.assert_not_called()

    def test_asana_error_counted(self):
        from cora.tools.asana_client import AsanaClientError
        slack = _make_slack()
        users = [_make_user()]
        with patch("cora.tools.asana_client.get_user_tasks", side_effect=AsanaClientError("401")):
            stats = mod.run_pass1_due_tasks(slack, users, {}, _az_now(), False)
        assert stats["errors"] == 1

    def test_user_without_gid_skipped(self):
        slack = _make_slack()
        users = [{"slack_user_id": "U001", "display_name": "No GID"}]
        with patch("cora.tools.asana_client.get_user_tasks") as mock_get:
            mod.run_pass1_due_tasks(slack, users, {}, _az_now(), False)
        mock_get.assert_not_called()

    def test_tomorrow_task_alerted(self):
        tomorrow = (_az_now() + timedelta(days=1)).strftime("%Y-%m-%d")
        slack, stats, _ = self._run(["task1"], due_on=tomorrow)
        assert stats["alerted"] == 1

    def test_dm_failure_does_not_raise(self):
        slack = _make_slack()
        slack.conversations_open.side_effect = Exception("DM error")
        now = _az_now()
        today = now.strftime("%Y-%m-%d")
        users = [_make_user()]
        with patch("cora.tools.asana_client.get_user_tasks", return_value=[_make_task(due_on=today)]):
            # Should not raise
            stats = mod.run_pass1_due_tasks(slack, users, {}, now, False)
        assert stats["alerted"] == 0

    def test_dm_text_contains_task_name(self):
        slack = _make_slack()
        now = _az_now()
        today = now.strftime("%Y-%m-%d")
        users = [_make_user()]
        task = _make_task(name="Review OSN P&L", due_on=today)
        with patch("cora.tools.asana_client.get_user_tasks", return_value=[task]):
            mod.run_pass1_due_tasks(slack, users, {}, now, False)
        text = slack.chat_postMessage.call_args.kwargs["text"]
        assert "Review OSN P&L" in text

    def test_dm_text_contains_due_date(self):
        slack = _make_slack()
        now = _az_now()
        today = now.strftime("%Y-%m-%d")
        users = [_make_user()]
        with patch("cora.tools.asana_client.get_user_tasks", return_value=[_make_task(due_on=today)]):
            mod.run_pass1_due_tasks(slack, users, {}, now, False)
        text = slack.chat_postMessage.call_args.kwargs["text"]
        assert today in text


# ---------------------------------------------------------------------------
# _parse_pending_decisions tests
# ---------------------------------------------------------------------------

class TestParsePendingDecisions:
    def test_parses_p0_lines(self, tmp_path):
        f = tmp_path / "decisions-pending.md"
        f.write_text(
            "# Decisions Pending\n"
            "- **P0** Harrison OIC pre-qualifier 2026-04-01\n"
            "- **P1** Something lower priority\n"
            "- **P0** OSN structure decision 2026-05-15\n",
            encoding="utf-8",
        )
        decisions = mod._parse_pending_decisions(f)
        assert len(decisions) == 2
        assert all("P0" in d["text"] for d in decisions)

    def test_missing_file_returns_empty(self, tmp_path):
        f = tmp_path / "nonexistent.md"
        assert mod._parse_pending_decisions(f) == []

    def test_extracts_date_from_line(self, tmp_path):
        f = tmp_path / "dp.md"
        f.write_text("- P0 something 2026-03-01\n", encoding="utf-8")
        decisions = mod._parse_pending_decisions(f)
        assert len(decisions) == 1
        assert decisions[0]["date"] is not None
        assert decisions[0]["date"].strftime("%Y-%m-%d") == "2026-03-01"

    def test_no_date_falls_back_to_none(self, tmp_path):
        f = tmp_path / "dp.md"
        f.write_text("- P0 no date in this line\n", encoding="utf-8")
        decisions = mod._parse_pending_decisions(f)
        assert len(decisions) == 1
        assert decisions[0]["date"] is None

    def test_non_p0_lines_excluded(self, tmp_path):
        f = tmp_path / "dp.md"
        f.write_text(
            "- P1 lower priority item\n"
            "- Regular line without priority\n",
            encoding="utf-8",
        )
        decisions = mod._parse_pending_decisions(f)
        assert len(decisions) == 0


# ---------------------------------------------------------------------------
# run_pass2_stalled_decisions tests
# ---------------------------------------------------------------------------

class TestPass2:
    def _decisions_file(self, tmp_path, content):
        f = tmp_path / "decisions-pending.md"
        f.write_text(content, encoding="utf-8")
        return f

    def test_stale_p0_alerts_harrison(self, tmp_path, monkeypatch):
        f = self._decisions_file(
            tmp_path,
            "- P0 Old decision 2025-01-01\n"
        )
        monkeypatch.setattr(mod, "_DECISIONS_PENDING_PATH", f)
        slack = _make_slack()
        stats = mod.run_pass2_stalled_decisions(slack, {}, dry_run=False)
        assert stats["alerted"] == 1
        slack.chat_postMessage.assert_called_once()

    def test_recent_p0_not_alerted(self, tmp_path, monkeypatch):
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        f = self._decisions_file(tmp_path, f"- P0 Recent decision {tomorrow}\n")
        monkeypatch.setattr(mod, "_DECISIONS_PENDING_PATH", f)
        slack = _make_slack()
        stats = mod.run_pass2_stalled_decisions(slack, {}, dry_run=False)
        assert stats["alerted"] == 0

    def test_throttled_decision_skipped(self, tmp_path, monkeypatch):
        f = self._decisions_file(tmp_path, "- P0 Stale decision 2025-01-01\n")
        monkeypatch.setattr(mod, "_DECISIONS_PENDING_PATH", f)
        import hashlib
        text = "- P0 Stale decision 2025-01-01"
        h = hashlib.md5(text.encode()).hexdigest()
        throttle = {f"decision:{h}": time.time() - 100}
        slack = _make_slack()
        stats = mod.run_pass2_stalled_decisions(slack, throttle, dry_run=False)
        assert stats["throttled"] == 1
        slack.chat_postMessage.assert_not_called()

    def test_dry_run_no_dm(self, tmp_path, monkeypatch):
        f = self._decisions_file(tmp_path, "- P0 Old decision 2025-01-01\n")
        monkeypatch.setattr(mod, "_DECISIONS_PENDING_PATH", f)
        slack = _make_slack()
        stats = mod.run_pass2_stalled_decisions(slack, {}, dry_run=True)
        assert stats["alerted"] == 1
        slack.chat_postMessage.assert_not_called()

    def test_no_file_returns_empty_stats(self, tmp_path, monkeypatch):
        f = tmp_path / "missing.md"
        monkeypatch.setattr(mod, "_DECISIONS_PENDING_PATH", f)
        slack = _make_slack()
        stats = mod.run_pass2_stalled_decisions(slack, {}, dry_run=False)
        assert stats["alerted"] == 0

    def test_dm_sent_to_harrison(self, tmp_path, monkeypatch):
        f = self._decisions_file(tmp_path, "- P0 Old decision 2025-01-01\n")
        monkeypatch.setattr(mod, "_DECISIONS_PENDING_PATH", f)
        slack = _make_slack()
        mod.run_pass2_stalled_decisions(slack, {}, dry_run=False)
        slack.conversations_open.assert_called_once_with(users=[mod._HARRISON_SLACK_ID])
