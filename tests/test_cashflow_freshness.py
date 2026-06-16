"""Tests for cashflow freshness detection (audit N1 / Phase 1.2).

The connector read is sound (verified live: it reads CF_SUMMARY and every tab
without truncation). These guard that a stale SHEET -- a week older than ~10 days
because Justin/Hayden have not updated it -- is detectable, so consumers can
surface it as stale rather than presenting an old week as the current number.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors import gsheets_financials as g  # noqa: E402

_TODAY = date(2026, 6, 16)


def _summary(week_label: str) -> "g.CashflowSummary":
    return g.CashflowSummary(week_label=week_label, as_of_date="unknown")


def test_parse_week_date_dash():
    assert g._parse_week_date("Week of 5-29", today=_TODAY) == date(2026, 5, 29)


def test_parse_week_date_slash_with_year():
    assert g._parse_week_date("Week of 5/29/2026", today=_TODAY) == date(2026, 5, 29)


def test_parse_week_date_infers_recent_past_year():
    # "12-30" read in early January should infer LAST year, never a future date.
    assert g._parse_week_date("Week of 12-30", today=date(2026, 1, 5)) == date(2025, 12, 30)


def test_parse_week_date_unparseable():
    assert g._parse_week_date("this week", today=_TODAY) is None


def test_data_age_days():
    assert _summary("Week of 5-29").data_age_days(today=_TODAY) == 18


def test_is_stale_true_for_old_week():
    # the live N1 case: a 5/29 week read on 6/16 is 18 days old -> stale.
    assert _summary("Week of 5-29").is_stale(today=_TODAY) is True


def test_is_stale_false_for_recent_week():
    assert _summary("Week of 6-15").is_stale(today=_TODAY) is False


def test_is_stale_fails_safe_when_unparseable():
    # an unparseable week is NOT flagged stale (avoid false alarms).
    assert _summary("this week").is_stale(today=_TODAY) is False
    assert _summary("this week").data_age_days(today=_TODAY) is None
