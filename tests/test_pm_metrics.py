"""PM-hub Phase 1 Slice 2: adoption instrumentation (pm_metrics).

log_pm_action (LEX aggregate-only), the weekly digest math (Cora-vs-UI, overdue WoW,
staleness, per-person), and the handler wiring. Invariant #2: a LEX task never
contributes a title to the log or the digest.
"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from cora import pm_metrics
import cora.tools.tool_dispatch as td

NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolate_pm_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(pm_metrics, "_ACTION_LOG", tmp_path / "pm-actions.jsonl")
    monkeypatch.setattr(pm_metrics, "_SNAPSHOT_DIR", tmp_path / "snaps")
    yield


def _ts(days_ago: float) -> int:
    return int(NOW.timestamp()) - int(days_ago * 86400)


def _write_actions(entries):
    path = pm_metrics._ACTION_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


# ─────────────────────────── log_pm_action ───────────────────────────
class TestLogAction:
    def test_writes_a_line(self):
        pm_metrics.log_pm_action("create", "U1", "F3E", "G1", title="Ship the deck")
        lines = pm_metrics._ACTION_LOG.read_text(encoding="utf-8").strip().split("\n")
        e = json.loads(lines[0])
        assert e["action"] == "create" and e["actor"] == "U1"
        assert e["entity"] == "F3E" and e["gid"] == "G1"

    def test_title_never_persisted_non_lex(self):
        # D-051: titles are omitted UNCONDITIONALLY (these tools act cross-entity, so the
        # channel entity can't gate a LEX title). Even a non-LEX title is not stored.
        pm_metrics.log_pm_action("create", "U1", "F3E", "G1", title="Ship the deck")
        e = json.loads(pm_metrics._ACTION_LOG.read_text(encoding="utf-8").strip())
        assert "title" not in e

    def test_lex_context_never_persists_title(self):
        pm_metrics.log_pm_action("complete", "U1", "LEX", "G2", title="Call John Doe's guardian")
        e = json.loads(pm_metrics._ACTION_LOG.read_text(encoding="utf-8").strip())
        assert "title" not in e and "John Doe" not in json.dumps(e)
        assert e["entity"] == "LEX" and e["gid"] == "G2"

    def test_cross_entity_lex_title_never_leaks(self):
        # The D-051 finding: a LEX task acted on from a FNDR/HJRG channel (entity='FNDR')
        # must NOT write the client-named title -- the previous channel-entity gate missed it.
        pm_metrics.log_pm_action("complete", "U0B2RM2JYJ1", "FNDR", "G9",
                                 title="Follow up on Jane Doe intake authorization")
        blob = pm_metrics._ACTION_LOG.read_text(encoding="utf-8")
        assert "Jane Doe" not in blob and '"title"' not in blob

    def test_extra_still_recorded(self):
        pm_metrics.log_pm_action("subtask", "U1", "F3E", "G1", extra={"parent": "P1"})
        e = json.loads(pm_metrics._ACTION_LOG.read_text(encoding="utf-8").strip())
        assert e["extra"] == {"parent": "P1"}

    def test_never_raises_on_write_error(self, monkeypatch):
        # point the log at an unwritable path shape; must swallow the error
        monkeypatch.setattr(pm_metrics, "_ACTION_LOG",
                            pm_metrics._ACTION_LOG / "cannot" / "be" / "a" / "dir")
        # should not raise
        pm_metrics.log_pm_action("create", "U1", "F3E", "G1", title="x")


# ─────────────────────────── read_actions ───────────────────────────
class TestReadActions:
    def test_window_filter(self):
        _write_actions([
            {"ts": _ts(2), "action": "create", "actor": "U1", "entity": "F3E", "gid": "a"},
            {"ts": _ts(10), "action": "create", "actor": "U1", "entity": "F3E", "gid": "b"},
        ])
        recent = pm_metrics.read_actions(_ts(7))
        assert [e["gid"] for e in recent] == ["a"]

    def test_skips_bad_lines(self):
        pm_metrics._ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        pm_metrics._ACTION_LOG.write_text(
            '{"ts": %d, "gid": "ok"}\nnot json\n' % _ts(1), encoding="utf-8")
        got = pm_metrics.read_actions(_ts(7))
        assert [e["gid"] for e in got] == ["ok"]


# ─────────────────────────── run() / digest math ───────────────────────────
def _asana_state():
    return {
        "open_total": 5, "overdue_total": 2, "stale_total": 1, "stale_days": 14,
        "created_this_week": 4, "completed_this_week": 3,
        "roster_covered": 3, "fetch_errors": 0,
        "per_person": [
            {"name": "Alice", "slack": "U1", "open": 3, "overdue": 1, "cora_actions": 2},
            {"name": "Bob", "slack": "U2", "open": 2, "overdue": 1, "cora_actions": 0},
        ],
    }


class TestRun:
    def test_buckets_cora_actions(self):
        _write_actions([
            {"ts": _ts(1), "action": "create", "actor": "U1", "entity": "F3E", "gid": "a"},
            {"ts": _ts(1), "action": "complete", "actor": "U1", "entity": "F3E", "gid": "b"},
            {"ts": _ts(2), "action": "subtask", "actor": "U2", "entity": "OSN", "gid": "c"},
            {"ts": _ts(9), "action": "create", "actor": "U1", "entity": "F3E", "gid": "old"},
        ])
        with patch.object(pm_metrics, "_gather_asana_state", return_value=_asana_state()):
            r = pm_metrics.run(now=NOW, write_state=False)
        assert r["cora"]["total_this_week"] == 3
        assert r["cora"]["total_prev_week"] == 1
        assert r["cora"]["created"] == 2   # create + subtask
        assert r["cora"]["completed"] == 1
        assert r["cora"]["by_entity"] == {"F3E": 2, "OSN": 1}
        assert r["cora"]["by_actor"]["U1"] == 2

    def test_asana_failure_is_fail_soft(self):
        _write_actions([{"ts": _ts(1), "action": "create", "actor": "U1", "entity": "F3E", "gid": "a"}])
        with patch.object(pm_metrics, "_gather_asana_state", side_effect=RuntimeError("boom")):
            r = pm_metrics.run(now=NOW, write_state=False)
        assert r["asana"] is None
        assert "boom" in r["asana_error"]
        assert r["cora"]["total_this_week"] == 1  # Cora metrics still deliver

    def test_overdue_wow_from_prior_snapshot(self):
        _write_actions([{"ts": _ts(1), "action": "create", "actor": "U1", "entity": "F3E", "gid": "a"}])
        pm_metrics._SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        (pm_metrics._SNAPSHOT_DIR / "2026-07-08.json").write_text(
            json.dumps({"date": "2026-07-08", "overdue_total": 5}), encoding="utf-8")
        with patch.object(pm_metrics, "_gather_asana_state", return_value=_asana_state()):
            r = pm_metrics.run(now=NOW, write_state=True)
        assert r["overdue_wow"]["delta"] == 2 - 5  # current 2 vs prior 5
        # today's snapshot was written
        assert (pm_metrics._SNAPSHOT_DIR / "2026-07-15.json").exists()

    def test_dry_run_writes_no_snapshot(self):
        _write_actions([{"ts": _ts(1), "action": "create", "actor": "U1", "entity": "F3E", "gid": "a"}])
        with patch.object(pm_metrics, "_gather_asana_state", return_value=_asana_state()):
            pm_metrics.run(now=NOW, write_state=False)
        assert not (pm_metrics._SNAPSHOT_DIR / "2026-07-15.json").exists()


# ─────────────────────────── _gather_asana_state ───────────────────────────
class TestGatherAsanaState:
    def _tasks(self):
        return [
            # open, created this week, overdue, fresh
            {"gid": "A", "completed": False, "created_at": "2026-07-10T00:00:00Z",
             "modified_at": "2026-07-14T00:00:00Z", "due_on": "2026-07-01"},
            # open, old, stale, no due
            {"gid": "B", "completed": False, "created_at": "2026-06-01T00:00:00Z",
             "modified_at": "2026-06-20T00:00:00Z", "due_on": None},
            # completed this week, created this week
            {"gid": "C", "completed": True, "completed_at": "2026-07-11T00:00:00Z",
             "created_at": "2026-07-09T00:00:00Z", "modified_at": "2026-07-11T00:00:00Z"},
        ]

    def test_counts(self):
        roster = [{"asana_user_gid": "g1", "slack_user_id": "U1", "display_name": "Alice"}]
        with patch.object(pm_metrics, "_load_roster", return_value=roster), \
             patch("cora.tools.asana_client.get_user_tasks", return_value=self._tasks()):
            state = pm_metrics._gather_asana_state(
                datetime(2026, 7, 8, tzinfo=timezone.utc), NOW, 14, {"U1": 3})
        assert state["open_total"] == 2          # A, B
        assert state["overdue_total"] == 1       # A
        assert state["stale_total"] == 1         # B
        assert state["created_this_week"] == 2   # A, C
        assert state["completed_this_week"] == 1  # C
        assert state["per_person"][0]["cora_actions"] == 3

    def test_fetch_error_tolerated(self):
        roster = [
            {"asana_user_gid": "g1", "slack_user_id": "U1", "display_name": "Alice"},
            {"asana_user_gid": "g2", "slack_user_id": "U2", "display_name": "Bob"},
        ]
        def _side(gid, **kw):
            if gid == "g2":
                raise RuntimeError("bad mailbox")
            return self._tasks()
        with patch.object(pm_metrics, "_load_roster", return_value=roster), \
             patch("cora.tools.asana_client.get_user_tasks", side_effect=_side):
            state = pm_metrics._gather_asana_state(
                datetime(2026, 7, 8, tzinfo=timezone.utc), NOW, 14, {})
        assert state["fetch_errors"] == 1
        assert state["open_total"] == 2  # g1's tasks still counted

    def test_skips_unmapped_roster_rows(self):
        roster = [
            {"slack_user_id": "U1", "display_name": "External Consultant"},  # no gid
            {"asana_user_gid": "g1", "slack_user_id": "U2", "display_name": "Alice"},
        ]
        with patch.object(pm_metrics, "_load_roster", return_value=roster), \
             patch("cora.tools.asana_client.get_user_tasks", return_value=[]) as m:
            pm_metrics._gather_asana_state(datetime(2026, 7, 8, tzinfo=timezone.utc), NOW, 14, {})
        assert m.call_count == 1  # only the mapped row fetched


# ─────────────────────────── format_digest ───────────────────────────
class TestFormatDigest:
    def _result(self, asana=True):
        return {
            "lookback_days": 7,
            "cora": {"total_this_week": 12, "total_prev_week": 8,
                     "by_action": {"create": 5, "complete": 4, "update": 3},
                     "by_action_prev": {}, "by_entity": {"F3E": 7, "LEX": 5},
                     "by_actor": {}, "created": 5, "completed": 4},
            "asana": _asana_state() if asana else None,
            "asana_error": None if asana else "conn down",
            "overdue_wow": {"current": 2, "prior": 5, "delta": -3},
        }

    def test_has_counts_and_wow(self):
        text = pm_metrics.format_digest(self._result())
        assert "12 actions" in text and "prev week: 8" in text
        assert "(-3 WoW)" in text
        assert "via Cora" in text and "directly in Asana" in text

    def test_never_leaks_a_task_title(self):
        # LEX aggregate-only: the digest is counts + display names, never a task title.
        text = pm_metrics.format_digest(self._result())
        assert "Alice" in text  # display name is fine
        # entity counts fine; no task-title field exists in the result to leak
        assert "title" not in text.lower()

    def test_asana_unavailable_still_renders(self):
        text = pm_metrics.format_digest(self._result(asana=False))
        assert "unavailable" in text.lower() and "12 actions" in text


# ─────────────────────────── handler wiring ───────────────────────────
class TestHandlerWiring:
    """Every task-write handler logs a Cora-attributed action on success."""

    @pytest.fixture(autouse=True)
    def _clear(self):
        td._PENDING_ASANA_WRITES.clear()
        yield
        td._PENDING_ASANA_WRITES.clear()

    def _drive_confirm(self, tool, first_input, entity="FNDR"):
        ch = "hjrg-leadership"
        tool("U0B2RM2JYJ1", entity, {**first_input, "_channel_name": ch})
        return tool("U0B2RM2JYJ1", entity, {"confirmed": True, "_channel_name": ch})

    def test_complete_logs(self):
        with patch.object(td.pm_metrics, "log_pm_action") as logm, \
             patch.object(td.asana_client, "complete_task", return_value={}):
            self._drive_confirm(td._tool_asana_complete_task, {"task_gid": "T1"})
        logm.assert_called_once()
        assert logm.call_args.args[0] == "complete"

    def test_delete_logs(self):
        with patch.object(td.pm_metrics, "log_pm_action") as logm, \
             patch.object(td.asana_client, "delete_task", return_value=None):
            self._drive_confirm(td._tool_asana_delete_task, {"task_gid": "T1"})
        logm.assert_called_once()
        assert logm.call_args.args[0] == "delete"

    def test_update_logs(self):
        with patch.object(td.pm_metrics, "log_pm_action") as logm, \
             patch.object(td.asana_client, "update_task", return_value={}):
            self._drive_confirm(td._tool_asana_update_task, {"task_gid": "T1", "new_due_on": "2026-08-01"})
        logm.assert_called_once()
        assert logm.call_args.args[0] == "update"

    def test_comment_logs(self):
        with patch.object(td.pm_metrics, "log_pm_action") as logm, \
             patch.object(td.asana_client, "create_task_comment", return_value={}):
            self._drive_confirm(td._tool_asana_add_comment, {"task_gid": "T1", "text": "hi"})
        logm.assert_called_once()
        assert logm.call_args.args[0] == "comment"

    def test_subtask_logs(self):
        with patch.object(td.pm_metrics, "log_pm_action") as logm, \
             patch.object(td.asana_client, "create_subtask", return_value={"gid": "S1"}):
            self._drive_confirm(td._tool_asana_add_subtask, {"parent_task_gid": "P1", "title": "step"})
        logm.assert_called_once()
        assert logm.call_args.args[0] == "subtask"

    def test_create_logs_with_entity(self):
        created = {"gid": "T9", "permalink_url": "http://x", "projects": []}
        with patch.object(td.pm_metrics, "log_pm_action") as logm, \
             patch.object(td.asana_client, "create_task", return_value=created), \
             patch.object(td.asana_client, "get_project_tasks", return_value=[]):
            td._tool_asana_create_task("U0B2RM2JYJ1", "F3E", {
                "title": "Ship it", "confirmed": True, "_channel_name": "f3e-leadership"})
        logm.assert_called_once()
        assert logm.call_args.args[0] == "create"
        assert logm.call_args.args[2] == "F3E"  # entity threaded through
