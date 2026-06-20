"""WS7 — multi-week ending-cash outlook extraction in gsheets_financials.

Verifies the connector surfaces a chronological ending-cash series (current +
forecast weeks) so the WS7 snapshot writer can expose a cash outlook to the
Cowork morning brief.
"""

from __future__ import annotations

import csv
import io

from cora.connectors.gsheets_financials import (
    CashflowSummary,
    EntityRow,
    _ordered_weeks,
    _parse_cashflow_csv,
)


def _multi_week_csv() -> str:
    """4 weeks; an entity has actuals in w1+w2 only (so latest-actual = w2).

    Ending-Cash row: w1/w2 actual, w3/w4 forecast-only.
    """
    weeks = ["6/02/2026", "6/09/2026", "6/16/2026", "6/23/2026"]
    date_row = [""]
    for w in weeks:
        date_row += [w, "", ""]
    header_row = ["Entity"]
    for _ in weeks:
        header_row += ["FORECAST", "ACTUAL", "DIFF"]
    # An entity with actuals only in w1+w2 -> latest-actual week is w2 (6/09).
    f3_row = ["F3", "3000", "1680", "-1320", "3000", "2000", "-1000",
              "3000", "", "", "3000", "", ""]
    # Ending Cash row: w1 act 100000, w2 act 110000, w3 fcst 90000, w4 fcst 80000.
    ec_row = ["Ending Cash/CC Book Balance",
              "", "100000", "", "", "110000", "", "90000", "", "", "80000", "", ""]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerows([date_row, header_row, f3_row, ec_row])
    return buf.getvalue()


def test_ordered_weeks_is_chronological():
    col_map = [("", "ENTITY"),
               ("6/02/2026", "FORECAST"), ("6/02/2026", "ACTUAL"),
               ("6/09/2026", "FORECAST"), ("6/09/2026", "ACTUAL")]
    assert _ordered_weeks(col_map) == ["6/02/2026", "6/09/2026"]


def test_ending_cash_series_populated_with_actual_flags():
    summary = _parse_cashflow_csv(_multi_week_csv(), "2026-06-16")
    series = summary.ending_cash_series
    assert [e["week"] for e in series] == ["6/02/2026", "6/09/2026", "6/16/2026", "6/23/2026"]
    by_week = {e["week"]: e for e in series}
    assert by_week["6/02/2026"]["ending_cash"] == 100000.0
    assert by_week["6/02/2026"]["is_actual"] is True
    assert by_week["6/16/2026"]["ending_cash"] == 90000.0
    assert by_week["6/16/2026"]["is_actual"] is False  # forecast-only
    assert by_week["6/23/2026"]["ending_cash"] == 80000.0


def test_ending_cash_outlook_anchors_on_current_week():
    summary = _parse_cashflow_csv(_multi_week_csv(), "2026-06-16")
    # Latest-actual week is 6/09 -> outlook(2) returns 6/09 + next 2 forecast weeks.
    outlook = summary.ending_cash_outlook(weeks=2)
    assert [e["week"] for e in outlook] == ["6/09/2026", "6/16/2026", "6/23/2026"]
    assert outlook[0]["ending_cash"] == 110000.0
    assert outlook[0]["is_actual"] is True
    assert outlook[-1]["is_actual"] is False


def test_outlook_empty_when_no_ending_cash_row():
    cs = CashflowSummary(week_label="Week of 6/09/2026", as_of_date="2026-06-16")
    assert cs.ending_cash_outlook() == []
    assert cs.ending_cash_series == []


def test_outlook_falls_back_to_start_when_target_not_in_series():
    cs = CashflowSummary(
        week_label="Week of 12/31/2099",  # not in the series
        as_of_date="2026-06-16",
        ending_cash_series=[
            {"week": "6/09/2026", "ending_cash": 1.0, "is_actual": True},
            {"week": "6/16/2026", "ending_cash": 2.0, "is_actual": False},
        ],
    )
    out = cs.ending_cash_outlook(weeks=1)
    assert [e["week"] for e in out] == ["6/09/2026", "6/16/2026"]
