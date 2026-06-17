"""Phase 1.7a: stale-task filter for the daily brief (N7 / Harrison #1).

The morning brief surfaced abandoned goal-tracking tasks ("Sales & Revenue
Goals due 2025-02-04") every day. ``asana_client.drop_stale_tasks`` removes
tasks overdue past a threshold, keeping no-due / recent / unparseable / P0
tasks. ``_plate_asana_section`` opts in only when ``drop_stale_days`` is set
(the brief opts in; the on-demand plate tool does not), so existing plate
behavior is unchanged and the filter is not time-coupled to the suite.

A fixed ``today`` is injected into the unit tests; the integration tests use a
2020 due date (always > 90 days old regardless of when the suite runs).
"""

from datetime import date
from unittest.mock import patch

from cora.tools import asana_client

_TODAY = date(2026, 6, 16)


def _t(name: str, due: str | None = None, notes: str = "") -> dict:
    d: dict = {"name": name, "notes": notes}
    if due is not None:
        d["due_on"] = due
    return d


class TestDropStaleTasks:
    def test_keeps_no_due_date(self):
        out = asana_client.drop_stale_tasks([_t("backlog")], today=_TODAY)
        assert [t["name"] for t in out] == ["backlog"]

    def test_keeps_future(self):
        out = asana_client.drop_stale_tasks([_t("soon", "2026-07-01")], today=_TODAY)
        assert len(out) == 1

    def test_keeps_recently_overdue(self):
        # ~46 days overdue -- still live work, not abandoned.
        out = asana_client.drop_stale_tasks([_t("recent", "2026-05-01")], today=_TODAY)
        assert len(out) == 1

    def test_drops_ancient(self):
        out = asana_client.drop_stale_tasks([_t("goal2025", "2025-02-04")], today=_TODAY)
        assert out == []

    def test_keeps_ancient_p0_in_name(self):
        out = asana_client.drop_stale_tasks([_t("[P0] critical", "2025-02-04")], today=_TODAY)
        assert len(out) == 1

    def test_keeps_ancient_p0_in_notes(self):
        out = asana_client.drop_stale_tasks(
            [_t("old", "2025-02-04", notes="severity: P0, escalate")], today=_TODAY
        )
        assert len(out) == 1

    def test_p0_must_be_word_bounded(self):
        # "SP00KY" / "P01" must NOT count as a P0 flag.
        out = asana_client.drop_stale_tasks(
            [_t("SP00KY backlog item P01", "2025-02-04")], today=_TODAY
        )
        assert out == []

    def test_keeps_unparseable_due(self):
        out = asana_client.drop_stale_tasks([_t("weird", "not-a-date")], today=_TODAY)
        assert len(out) == 1

    def test_parses_due_at_datetime(self):
        out = asana_client.drop_stale_tasks(
            [{"name": "old", "due_at": "2025-01-15T17:00:00.000Z", "notes": ""}], today=_TODAY
        )
        assert out == []

    def test_boundary_exactly_at_cutoff_is_kept(self):
        # Exactly max_overdue_days ago == cutoff, kept (>= cutoff).
        cutoff = date(2026, 3, 18)  # 90 days before 2026-06-16
        out = asana_client.drop_stale_tasks(
            [_t("edge", cutoff.isoformat())], today=_TODAY, max_overdue_days=90
        )
        assert len(out) == 1

    def test_one_day_past_cutoff_is_dropped(self):
        out = asana_client.drop_stale_tasks(
            [_t("just-over", "2026-03-17")], today=_TODAY, max_overdue_days=90
        )
        assert out == []

    def test_mixed_batch(self):
        tasks = [
            _t("keep-nodue"),
            _t("keep-future", "2026-12-01"),
            _t("drop-ancient", "2024-01-01"),
            _t("keep-p0", "2024-01-01", notes="P0"),
        ]
        out = {t["name"] for t in asana_client.drop_stale_tasks(tasks, today=_TODAY)}
        assert out == {"keep-nodue", "keep-future", "keep-p0"}

    def test_default_today_does_not_crash(self):
        # Default-now path: a no-due task is always kept regardless of clock.
        out = asana_client.drop_stale_tasks([_t("backlog")])
        assert len(out) == 1


class TestPlateSectionOptIn:
    """The stale filter only runs in _plate_asana_section when opted in."""

    _MAPPING = {"U_X": {"asana_user_gid": "123"}}

    def _tasks(self):
        # "2020-01-01" is always > 90 days old -> stable across run dates.
        return [
            {"name": "ancient", "due_on": "2020-01-01", "projects": [{"name": "[F3E] S"}]},
            {"name": "backlog", "projects": [{"name": "[F3E] S"}]},
        ]

    def test_plate_drops_stale_when_opted_in(self):
        import cora.tools.tool_dispatch as td

        with patch.object(td, "_load_slack_asana_map", return_value=self._MAPPING), \
             patch.object(td.asana_client, "get_user_tasks", return_value=self._tasks()), \
             patch.object(td.asana_client, "format_tasks_for_llm", return_value="OK") as fmt:
            td._plate_asana_section("U_X", "F3E", 90)
        shown = [t["name"] for t in fmt.call_args[0][0]]
        assert "ancient" not in shown
        assert "backlog" in shown

    def test_plate_keeps_stale_by_default(self):
        import cora.tools.tool_dispatch as td

        with patch.object(td, "_load_slack_asana_map", return_value=self._MAPPING), \
             patch.object(td.asana_client, "get_user_tasks", return_value=self._tasks()), \
             patch.object(td.asana_client, "format_tasks_for_llm", return_value="OK") as fmt:
            td._plate_asana_section("U_X", "F3E")  # no opt-in
        shown = [t["name"] for t in fmt.call_args[0][0]]
        assert "ancient" in shown
        assert "backlog" in shown
