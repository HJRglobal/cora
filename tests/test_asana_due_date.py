"""Slice 4 (2026-07-29 audit): asana_create_task natural-language due-date resolution.

The live bug: a task due "tomorrow" (from a 2026-07-28 context) was created with due
date 2025-07-29 -- one year stale -- because the model pre-resolved the word against its
own notion of "now" and the tool only shape-checked the result. The fix resolves relative
phrases server-side against the LIVE date and flags a far-past resolved date for
re-confirmation. Explicit YYYY-MM-DD passes through unchanged.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import cora.tools.tool_dispatch as td

# A fixed "now": Tuesday 2026-07-28, Arizona time.
_NOW = datetime(2026, 7, 28, 10, 0, tzinfo=timezone(timedelta(hours=-7)))


class TestResolveRelativeDueDate:
    def test_tomorrow_uses_current_year_not_stale(self):
        iso, warn = td._resolve_relative_due_date("tomorrow", now=_NOW)
        assert iso == "2026-07-29"  # the exact bug: NOT 2025-07-29
        assert warn is None

    def test_today(self):
        assert td._resolve_relative_due_date("today", now=_NOW)[0] == "2026-07-28"

    def test_yesterday(self):
        assert td._resolve_relative_due_date("yesterday", now=_NOW)[0] == "2026-07-27"

    def test_next_week(self):
        assert td._resolve_relative_due_date("next week", now=_NOW)[0] == "2026-08-04"

    def test_in_n_days(self):
        assert td._resolve_relative_due_date("in 3 days", now=_NOW)[0] == "2026-07-31"
        assert td._resolve_relative_due_date("5 days", now=_NOW)[0] == "2026-08-02"

    def test_weekday_name_is_future_and_correct_dow(self):
        iso, _ = td._resolve_relative_due_date("friday", now=_NOW)
        d = date.fromisoformat(iso)
        assert d.weekday() == 4 and d > _NOW.date()  # a future Friday

    def test_explicit_iso_unchanged(self):
        iso, warn = td._resolve_relative_due_date("2026-08-15", now=_NOW)
        assert iso == "2026-08-15" and warn is None

    def test_none_and_blank_are_no_due_date(self):
        assert td._resolve_relative_due_date(None, now=_NOW) == (None, None)
        assert td._resolve_relative_due_date("   ", now=_NOW) == (None, None)

    def test_unrecognized_dropped_with_warning(self):
        iso, warn = td._resolve_relative_due_date("someday soonish", now=_NOW)
        assert iso is None and warn and "didn't recognize" in warn.lower()

    def test_bad_iso_shape_dropped(self):
        iso, warn = td._resolve_relative_due_date("2026-13-40", now=_NOW)
        assert iso is None and warn


class TestDueDatePastWarning:
    def test_stale_year_flagged_with_plus_one_year_hint(self):
        w = td._due_date_past_warning("2025-07-29", now=_NOW)
        assert w and "past" in w and "2026-07-29" in w  # suggests the year-off fix

    def test_recent_past_not_flagged(self):
        assert td._due_date_past_warning("2026-07-01", now=_NOW) is None  # ~27d ago

    def test_future_not_flagged(self):
        assert td._due_date_past_warning("2026-12-01", now=_NOW) is None

    def test_far_backdate_flagged_without_hint(self):
        w = td._due_date_past_warning("2023-01-01", now=_NOW)
        assert w and "past" in w and "Did you mean" not in w
