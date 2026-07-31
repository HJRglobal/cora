"""Tests for Feature #6: Asana Due-Date DM Escalation."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Add scripts/ and src/ to sys.path for direct import (consistent with Tier 3 test pattern)
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

import run_due_date_escalation as mod  # noqa: E402


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
# -- patch.object(mod, "get_user_tasks") because mod uses direct from-import
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

        with patch.object(mod, "get_user_tasks", return_value=user_tasks):
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
        with patch.object(mod, "get_user_tasks", side_effect=AsanaClientError("401")):
            stats = mod.run_pass1_due_tasks(slack, users, {}, _az_now(), False)
        assert stats["errors"] == 1

    def test_user_without_gid_skipped(self):
        slack = _make_slack()
        users = [{"slack_user_id": "U001", "display_name": "No GID"}]
        with patch.object(mod, "get_user_tasks") as mock_get:
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
        with patch.object(mod, "get_user_tasks", return_value=[_make_task(due_on=today)]):
            # Should not raise
            stats = mod.run_pass1_due_tasks(slack, users, {}, now, False)
        assert stats["alerted"] == 0

    def test_dm_text_contains_task_name(self):
        slack = _make_slack()
        now = _az_now()
        today = now.strftime("%Y-%m-%d")
        users = [_make_user()]
        task = _make_task(name="Review OSN P&L", due_on=today)
        with patch.object(mod, "get_user_tasks", return_value=[task]):
            mod.run_pass1_due_tasks(slack, users, {}, now, False)
        text = slack.chat_postMessage.call_args.kwargs["text"]
        assert "Review OSN P&L" in text

    def test_dm_text_contains_due_date(self):
        slack = _make_slack()
        now = _az_now()
        today = now.strftime("%Y-%m-%d")
        users = [_make_user()]
        with patch.object(mod, "get_user_tasks", return_value=[_make_task(due_on=today)]):
            mod.run_pass1_due_tasks(slack, users, {}, now, False)
        text = slack.chat_postMessage.call_args.kwargs["text"]
        assert today in text


# ---------------------------------------------------------------------------
# _parse_pending_decisions tests
# ---------------------------------------------------------------------------

class TestParsePendingDecisions:
    """Rider B: parses the REAL Founder-OS '### topic' format via drive_io
    (the old list-marker+P0 parser false-matched the template/rubric and read the
    date off the wrong line; the path pointed at a non-existent repo memory/)."""

    def _fixture(self, fresh_date):
        # Mirrors the real file: the '### [Topic]' skeleton (with the
        # 'P0 / P1 / P2 / P3' alternatives line + the '- **P0**:' rubric), a
        # genuine stale P0, a fresh P1, and a '## Recently resolved' section.
        return (
            "# Pending Decisions Queue\n\n"
            "## How to use\n\n"
            "### [Topic]\n"
            "- **Severity**: P0 / P1 / P2 / P3\n"
            "- **Last touched**: YYYY-MM-DD\n\n"
            "Severity rubric:\n"
            "- **P0**: Decision must happen this week\n"
            "- **P1**: Decision must happen this month\n\n"
            "---\n\n"
            "## Active (as of 2026-07-20)\n\n"
            "### A genuinely stale P0 decision\n"
            "- **Entity**: FNDR\n"
            "- **Severity**: P0 (open ~3 months)\n"
            "- **Last touched**: 2025-01-01\n\n"
            "### A fresh P1 decision\n"
            "- **Entity**: F3E\n"
            "- **Severity**: P1\n"
            f"- **Last touched**: {fresh_date}\n\n"
            "## Recently resolved\n\n"
            "### This was resolved and must be ignored\n"
            "- **Severity**: P0\n"
            "- **Last touched**: 2025-01-01\n"
        )

    def _parse(self, monkeypatch, content):
        from cora import drive_io
        monkeypatch.setattr(drive_io, "read_text", lambda *a, **k: content)
        return mod._parse_pending_decisions(Path("decisions-pending.md"))

    def test_parses_real_format_p0_and_p1(self, monkeypatch):
        fresh = _az_now().date().isoformat()
        decisions = self._parse(monkeypatch, self._fixture(fresh))
        # skeleton + resolved excluded; the stale P0 + fresh P1 remain
        assert len(decisions) == 2
        assert {d["severity"] for d in decisions} == {"P0", "P1"}

    def test_skeleton_and_rubric_not_matched(self, monkeypatch):
        content = (
            "## How to use\n\n"
            "### [Topic]\n"
            "- **Severity**: P0 / P1 / P2 / P3\n\n"
            "Severity rubric:\n"
            "- **P0**: Decision must happen this week\n"
        )
        assert self._parse(monkeypatch, content) == []

    def test_recently_resolved_excluded(self, monkeypatch):
        content = (
            "## Active\n\n"
            "### Live one\n- **Severity**: P0\n- **Last touched**: 2025-01-01\n\n"
            "## Recently resolved\n\n"
            "### Dead one\n- **Severity**: P0\n- **Last touched**: 2025-01-01\n"
        )
        decisions = self._parse(monkeypatch, content)
        assert len(decisions) == 1
        assert "Live one" in decisions[0]["topic"]

    def test_p2_p3_excluded(self, monkeypatch):
        content = ("## Active\n\n### A P2 item\n- **Severity**: P2\n"
                   "- **Last touched**: 2025-01-01\n")
        assert self._parse(monkeypatch, content) == []

    def test_phi_topic_skipped(self, monkeypatch):
        # D-051 defense-in-depth: a PHI-flagged decision topic is never itemized.
        content = ("## Active\n\n### patient diagnosis review for a client\n"
                   "- **Severity**: P0\n- **Last touched**: 2025-01-01\n")
        assert self._parse(monkeypatch, content) == []

    def test_visibility_cpa_topic_skipped(self, monkeypatch):
        content = ("## Active\n\n### Visibility CPA fee decision\n"
                   "- **Severity**: P0\n- **Last touched**: 2025-01-01\n")
        assert self._parse(monkeypatch, content) == []

    def test_month_only_last_touched_dated(self, monkeypatch):
        # '~YYYY-MM' -> first of month so a coarsely-dated stale P0 still ages
        # (D-051: the '~2026-04' 1040 OIC P0 was silently never escalated).
        content = ("## Active\n\n### Old month-only P0\n"
                   "- **Severity**: P0\n- **Last touched**: ~2020-01\n")
        decisions = self._parse(monkeypatch, content)
        assert len(decisions) == 1
        assert decisions[0]["age_days"] is not None
        assert decisions[0]["age_days"] > 30

    def test_drive_unavailable_returns_empty(self, monkeypatch):
        from cora import drive_io
        def _boom(*a, **k):
            raise drive_io.DriveUnavailable("mount gone")
        monkeypatch.setattr(drive_io, "read_text", _boom)
        assert mod._parse_pending_decisions(Path("decisions-pending.md")) == []

    def test_file_not_found_returns_empty(self, monkeypatch):
        from cora import drive_io
        def _nf(*a, **k):
            raise FileNotFoundError("nope")
        monkeypatch.setattr(drive_io, "read_text", _nf)
        assert mod._parse_pending_decisions(Path("x.md")) == []

    def test_default_path_targets_founder_os(self):
        # The Rider B fix: default path is the G: Founder-OS file, NOT repo memory/.
        p = str(mod._DECISIONS_PENDING_PATH)
        assert "HJR-Founder-OS" in p and "memory" in p
        assert not p.startswith(str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# run_pass2_stalled_decisions tests
# ---------------------------------------------------------------------------

class TestPass2:
    def _patch(self, monkeypatch, content):
        from cora import drive_io
        monkeypatch.setattr(drive_io, "read_text", lambda *a, **k: content)

    def _entry(self, severity, last_touched, topic="Some decision"):
        return (f"## Active\n\n### {topic}\n- **Severity**: {severity}\n"
                f"- **Last touched**: {last_touched}\n")

    def test_stale_p0_alerts_harrison(self, monkeypatch):
        self._patch(monkeypatch, self._entry("P0", "2025-01-01"))
        slack = _make_slack()
        stats = mod.run_pass2_stalled_decisions(slack, {}, dry_run=False)
        assert stats["alerted"] == 1
        slack.chat_postMessage.assert_called_once()

    def test_stale_p1_also_alerts(self, monkeypatch):
        # Rider B: P1 (not just P0) now escalates, matching strategy_memo.
        self._patch(monkeypatch, self._entry("P1", "2025-01-01"))
        slack = _make_slack()
        stats = mod.run_pass2_stalled_decisions(slack, {}, dry_run=False)
        assert stats["alerted"] == 1

    def test_recent_p0_not_alerted(self, monkeypatch):
        fresh = _az_now().date().isoformat()
        self._patch(monkeypatch, self._entry("P0", fresh))
        slack = _make_slack()
        stats = mod.run_pass2_stalled_decisions(slack, {}, dry_run=False)
        assert stats["alerted"] == 0

    def test_undated_not_alerted(self, monkeypatch):
        # No Last touched -> age unknown -> not escalated (no false alert; the old
        # file-mtime fallback was itself broken since the file is edited often).
        self._patch(monkeypatch, "## Active\n\n### No date\n- **Severity**: P0\n")
        slack = _make_slack()
        stats = mod.run_pass2_stalled_decisions(slack, {}, dry_run=False)
        assert stats["alerted"] == 0

    def test_month_only_stale_p0_escalates(self, monkeypatch):
        # D-051 fix: a '~YYYY-MM' month-only stale P0 now escalates (was skipped).
        self._patch(monkeypatch, self._entry("P0", "~2020-01"))
        slack = _make_slack()
        stats = mod.run_pass2_stalled_decisions(slack, {}, dry_run=False)
        assert stats["alerted"] == 1

    def test_phi_topic_never_dmd(self, monkeypatch):
        # A PHI-flagged stale P0 is dropped by the filter -> no DM, no dry-run log.
        self._patch(monkeypatch, "## Active\n\n### patient diagnosis for a client\n"
                    "- **Severity**: P0\n- **Last touched**: 2020-01-01\n")
        slack = _make_slack()
        stats = mod.run_pass2_stalled_decisions(slack, {}, dry_run=False)
        assert stats["alerted"] == 0
        slack.chat_postMessage.assert_not_called()

    def test_dm_wording_never_narrates_touch_staleness_as_open_time(self, monkeypatch):
        """cq-935a18e2969e: the DM said 'open for {N}+ days' where N was days since
        Last touched — a 7/20 verify touch made a months-old item read as ~7d open.
        The trigger stays on staleness (by design), but the wording must say
        'untouched' and state true open age from Surfaced when known."""
        self._patch(monkeypatch,
                    "## Active\n\n### Old verified item\n- **Severity**: P0\n"
                    "- **Surfaced**: 2025-01-01\n"
                    "- **Last touched**: 2025-06-01 (VERIFIED STILL LIVE)\n")
        slack = _make_slack()
        stats = mod.run_pass2_stalled_decisions(slack, {}, dry_run=False)
        assert stats["alerted"] == 1
        msg = slack.chat_postMessage.call_args.kwargs.get("text") or \
            slack.chat_postMessage.call_args[1].get("text", "")
        assert "Open " in msg and "since 2025-01-01" in msg
        assert "untouched" in msg
        assert "been open for" not in msg   # the old conflated copy

    def test_dm_wording_without_surfaced_says_origination_unknown(self, monkeypatch):
        self._patch(monkeypatch, self._entry("P0", "2025-01-01"))
        slack = _make_slack()
        mod.run_pass2_stalled_decisions(slack, {}, dry_run=False)
        msg = slack.chat_postMessage.call_args.kwargs.get("text") or \
            slack.chat_postMessage.call_args[1].get("text", "")
        assert "origination unknown" in msg
        assert "untouched" in msg

    def test_throttled_decision_skipped(self, monkeypatch):
        import hashlib
        topic = "Some decision"
        self._patch(monkeypatch, self._entry("P0", "2025-01-01", topic))
        h = hashlib.md5(f"P0:{topic}".encode()).hexdigest()
        throttle = {f"decision:{h}": time.time() - 100}
        slack = _make_slack()
        stats = mod.run_pass2_stalled_decisions(slack, throttle, dry_run=False)
        assert stats["throttled"] == 1
        slack.chat_postMessage.assert_not_called()

    def test_dry_run_no_dm(self, monkeypatch):
        self._patch(monkeypatch, self._entry("P0", "2025-01-01"))
        slack = _make_slack()
        stats = mod.run_pass2_stalled_decisions(slack, {}, dry_run=True)
        assert stats["alerted"] == 1
        slack.chat_postMessage.assert_not_called()

    def test_drive_unavailable_no_crash(self, monkeypatch):
        from cora import drive_io
        def _boom(*a, **k):
            raise drive_io.DriveUnavailable("gone")
        monkeypatch.setattr(drive_io, "read_text", _boom)
        slack = _make_slack()
        stats = mod.run_pass2_stalled_decisions(slack, {}, dry_run=False)
        assert stats == {"alerted": 0, "throttled": 0}
        slack.chat_postMessage.assert_not_called()

    def test_dm_sent_to_harrison(self, monkeypatch):
        self._patch(monkeypatch, self._entry("P0", "2025-01-01"))
        slack = _make_slack()
        mod.run_pass2_stalled_decisions(slack, {}, dry_run=False)
        slack.conversations_open.assert_called_once_with(users=[mod._HARRISON_SLACK_ID])
