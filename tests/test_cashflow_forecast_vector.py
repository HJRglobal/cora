"""Forecast-vector accessor (13WCF shadow ledger M1/S1).

Two jobs:
  1. Pin the ADDITIVE contract — the accessor itself.
  2. Pin the NO-CHANGE contract — every pre-existing accessor, frozenset and
     helper this module already exposes to live finance surfaces is untouched.
     The shadow ledger must not be able to move a figure the pack already renders.

Fixture shape is copied from the live sheet (verified 2026-08-05): column-header
row ABOVE the date row, FORECAST/ACTUAL/DIFF triplet per week, accounting dashes
for zero, and bare "M-D" week labels with no year.
"""

from __future__ import annotations

from datetime import date

import pytest

from cora.connectors import gsheets_financials as gf


# ── fixtures ────────────────────────────────────────────────────────────────

def _tab_csv(
    weeks: list[str],
    ending: list[tuple[str, str, str]],
    *,
    netflow: list[tuple[str, str, str]] | None = None,
    beginning: list[tuple[str, str, str]] | None = None,
    include_decoy: bool = True,
) -> str:
    """Build a CF-tab CSV in the live layout.

    `ending` etc. are per-week (forecast, actual, diff) display strings.
    """
    def triplet_row(label: str, cells: list[tuple[str, str, str]]) -> str:
        out = [label, "", "", "", ""]
        for f, a, d in cells:
            out += [f, a, d]
        return ",".join(f'"{c}"' for c in out)

    header = ["CF_TEST", "", "", "", ""]
    for _ in weeks:
        header += ["FORECAST", "ACTUAL", "DIFF"]
    dates = ["", "Ownership %", "", "100%", ""]
    for w in weeks:
        dates += [w, w, ""]

    rows = [
        '"HJR_Harrison\'s COMPANIES"',
        ",".join(f'"{c}"' for c in header),
        "",
        "",
        ",".join(f'"{c}"' for c in dates),
        "",
        '"Cash Receipts"',
    ]
    if netflow:
        rows.append(triplet_row("Net Cash Flow", netflow))
    if beginning:
        rows.append(triplet_row("BEGINNING Cash/CC - Book Balance", beginning))
    rows.append(triplet_row("Ending Cash/CC Book Balance", ending))
    if include_decoy:
        # The real sheet carries this zero-valued decoy AFTER the true row.
        rows.append(triplet_row(
            "Total Liquidity - ENDING Cash/CC - Book Balance-S/B ZERO",
            [("0.00", "0.00", "")] * len(weeks),
        ))
    return "\n".join(rows)


TODAY = date(2026, 8, 5)


# ── week-grid resolution ────────────────────────────────────────────────────

class TestResolveWeekEndings:
    def test_forward_weeks_get_the_correct_year(self):
        """The bug this function exists to prevent.

        _parse_week_date maps a forward "10-30" to LAST year (most-recent-past
        rule). The ledger is keyed by absolute week-endings, so that would stamp
        every forecast week a year in the past.
        """
        labels = ["7-31", "8-7", "8-14"]
        endings, _ = gf.resolve_week_endings(labels, today=TODAY)
        assert [d.isoformat() for d in endings] == [
            "2026-07-31", "2026-08-07", "2026-08-14",
        ]
        # And the old helper really does get it wrong — pinned so nobody
        # "simplifies" the walk back into _parse_week_date.
        assert gf._parse_week_date("Week of 8-7", today=TODAY) == date(2025, 8, 7)
        assert gf._parse_week_date("Week of 10-30", today=TODAY) == date(2025, 10, 30)

    def test_a_forward_week_does_not_steal_the_anchor(self):
        """"8-7" also resolves into the past (2025-08-07) under the year-1
        fallback. If it were allowed to anchor, the whole grid would shift back
        a year — which is exactly what the live 55-week grid produced."""
        labels = ["7-24", "7-31", "8-7", "8-14"]
        endings, _ = gf.resolve_week_endings(labels, today=TODAY)
        assert endings[0].year == 2026 and endings[-1].isoformat() == "2026-08-14"

    def test_grid_entirely_in_the_future(self):
        """A tab with no completed week must still resolve, not raise."""
        endings, weekday = gf.resolve_week_endings(["8-7", "8-14"], today=TODAY)
        assert [d.isoformat() for d in endings] == ["2026-08-07", "2026-08-14"]
        assert weekday == "Friday"

    def test_spans_a_backward_year_boundary(self):
        labels = ["12-26", "1-2", "1-9"]
        endings, _ = gf.resolve_week_endings(labels, today=date(2026, 2, 1))
        assert [d.isoformat() for d in endings] == [
            "2025-12-26", "2026-01-02", "2026-01-09",
        ]

    def test_full_live_grid_shape(self):
        """55 Friday weeks, 2025-10-17 .. 2026-10-30 (the live grid)."""
        labels = []
        d = date(2025, 10, 17)
        while d <= date(2026, 10, 30):
            labels.append(f"{d.month}-{d.day}")
            d = date.fromordinal(d.toordinal() + 7)
        endings, weekday = gf.resolve_week_endings(labels, today=TODAY)
        assert len(endings) == 55
        assert weekday == "Friday"
        assert endings[0].isoformat() == "2025-10-17"
        assert endings[-1].isoformat() == "2026-10-30"

    def test_weekday_is_derived_not_assumed(self):
        """Fin-13: no hardcoded Friday. This grid ends on Thursdays."""
        _, weekday = gf.resolve_week_endings(["7-30", "8-6", "8-13"], today=TODAY)
        assert weekday == "Thursday"

    def test_mixed_weekdays_rejected(self):
        with pytest.raises(gf.WeekGridError, match="multiple weekdays"):
            gf.resolve_week_endings(["8-7", "8-15"], today=TODAY)

    def test_non_weekly_spacing_rejected(self):
        """A skipped column must fail loudly, not silently mis-key the ledger."""
        with pytest.raises(gf.WeekGridError, match="7 days apart"):
            gf.resolve_week_endings(["7-31", "8-14"], today=TODAY)

    def test_unparseable_label_rejected(self):
        with pytest.raises(gf.WeekGridError, match="unparseable"):
            gf.resolve_week_endings(["7-31", "TOTAL"], today=TODAY)

    def test_empty_rejected(self):
        with pytest.raises(gf.WeekGridError):
            gf.resolve_week_endings([], today=TODAY)


# ── accounting-cell parsing ─────────────────────────────────────────────────

class TestParseAccountingCell:
    def test_dash_is_zero_blank_is_unknown(self):
        """UNKNOWN is never zero (D-117) — and zero is never UNKNOWN."""
        assert gf._parse_accounting_cell("- ") == 0.0
        assert gf._parse_accounting_cell("$-") == 0.0
        assert gf._parse_accounting_cell("") is None
        assert gf._parse_accounting_cell("   ") is None
        assert gf._parse_accounting_cell(None) is None

    def test_money_and_parens(self):
        assert gf._parse_accounting_cell("823,570 ") == 823570.0
        assert gf._parse_accounting_cell("($51,453)") == -51453.0
        assert gf._parse_accounting_cell(" 104,795 ") == 104795.0

    def test_existing_parse_float_is_unchanged(self):
        """_parse_float still collapses the dash to None for its own callers."""
        assert gf._parse_float("- ") is None
        assert gf._parse_float("823,570") == 823570.0


# ── vector parsing ──────────────────────────────────────────────────────────

class TestParseForecastVector:
    def test_happy_path_labels_basis_and_forward_window(self):
        csv_text = _tab_csv(
            ["7-24", "7-31", "8-7", "8-14"],
            ending=[
                ("100", "100", "- "),      # closed
                ("200", "200", "- "),      # closed
                ("300", "", ""),           # forward
                ("400", "", ""),           # forward
            ],
        )
        v = gf.parse_forecast_vector(csv_text, "CF_TEST", today=TODAY)
        assert v.ok, v.unknown_reason
        assert v.week_ending_weekday == "Friday"
        pts = v.series["ending_cash"]
        assert [p.week_ending for p in pts] == [
            "2026-07-24", "2026-07-31", "2026-08-07", "2026-08-14",
        ]
        # D-121: a closed week's forecast cell is NOT a forecast.
        assert [p.basis for p in pts] == [
            gf.BASIS_POST_CLOSE, gf.BASIS_POST_CLOSE,
            gf.BASIS_FORECAST, gf.BASIS_FORECAST,
        ]
        assert v.last_actual_week_ending == "2026-07-31"
        assert v.forward_week_endings == ["2026-08-07", "2026-08-14"]

    def test_zero_forecast_survives_as_zero(self):
        csv_text = _tab_csv(["8-7"], ending=[("- ", "", "")])
        v = gf.parse_forecast_vector(csv_text, "CF_TEST", today=TODAY)
        assert v.ok
        assert v.series["ending_cash"][0].forecast == 0.0

    def test_triplet_misalignment_makes_the_tab_unknown(self):
        """Fin-12: a wrong column must never render as a figure."""
        csv_text = _tab_csv(
            ["7-24", "7-31"],
            ending=[("100", "100", "- "), ("200", "900", "- ")],
        )
        v = gf.parse_forecast_vector(csv_text, "CF_TEST", today=TODAY)
        assert not v.ok
        assert "triplet self-check failed" in v.unknown_reason
        assert v.series == {}          # no partial series leaks out

    def test_rounding_residual_is_tolerated(self):
        csv_text = _tab_csv(["7-31"], ending=[("100", "101", "2")])
        v = gf.parse_forecast_vector(csv_text, "CF_TEST", today=TODAY)
        assert v.ok
        assert v.triplet_worst_residual == pytest.approx(1.0)

    def test_all_three_measures_captured(self):
        csv_text = _tab_csv(
            ["7-31"],
            ending=[("200", "200", "- ")],
            netflow=[("-50", "-50", "- ")],
            beginning=[("250", "250", "")],
        )
        v = gf.parse_forecast_vector(csv_text, "CF_TEST", today=TODAY)
        assert v.ok
        assert set(v.series) == {"ending_cash", "net_cash_flow", "beginning_cash"}
        assert v.series["net_cash_flow"][0].actual == -50.0
        assert v.missing_measures == []

    def test_missing_optional_measure_is_recorded_not_fatal(self):
        csv_text = _tab_csv(["7-31"], ending=[("200", "200", "- ")])
        v = gf.parse_forecast_vector(csv_text, "CF_TEST", today=TODAY)
        assert v.ok
        assert sorted(v.missing_measures) == ["beginning_cash", "net_cash_flow"]

    def test_missing_ending_cash_row_is_unknown(self):
        csv_text = _tab_csv(["7-31"], ending=[("1", "1", "- ")]).replace(
            "Ending Cash/CC Book Balance", "Something Else"
        )
        v = gf.parse_forecast_vector(csv_text, "CF_TEST", today=TODAY)
        assert not v.ok
        assert "Ending Cash" in v.unknown_reason

    def test_zero_decoy_row_is_not_the_ending_cash_row(self):
        """The live sheet's 'Total Liquidity - ENDING Cash/CC - Book Balance-S/B
        ZERO' row is all zeros; picking it up would render every entity flat."""
        csv_text = _tab_csv(["7-31"], ending=[("53,342", "53,342", "- ")])
        v = gf.parse_forecast_vector(csv_text, "CF_TEST", today=TODAY)
        assert v.ok
        assert v.series["ending_cash"][0].actual == 53342.0

    def test_per_tab_forward_boundary(self):
        """Verified live: CF_HJR Prop carried an actual for 8-7 while every other
        tab stopped at 7-31. A global week-0 would mis-slice this tab."""
        csv_text = _tab_csv(
            ["7-31", "8-7", "8-14"],
            ending=[("100", "100", "- "), ("200", "200", "- "), ("300", "", "")],
        )
        v = gf.parse_forecast_vector(csv_text, "CF_TEST", today=TODAY)
        assert v.last_actual_week_ending == "2026-08-07"
        assert v.forward_week_endings == ["2026-08-14"]

    def test_unparseable_grid_is_unknown_not_an_exception(self):
        v = gf.parse_forecast_vector("garbage,rows\nwith,no,header", "CF_TEST", today=TODAY)
        assert not v.ok
        assert v.series == {}

    def test_as_dict_is_json_shaped(self):
        csv_text = _tab_csv(["7-31", "8-7"], ending=[("1", "1", "- "), ("2", "", "")])
        d = gf.parse_forecast_vector(csv_text, "CF_TEST", today=TODAY).as_dict()
        import json
        json.dumps(d)  # must not raise
        assert d["forward_weeks"] == 1
        assert d["series"]["ending_cash"][0]["basis"] == gf.BASIS_POST_CLOSE


# ── NO-CHANGE pins on the pre-existing surface ──────────────────────────────

class TestExistingSurfaceUnchanged:
    """The shadow ledger is additive. These pin the accessors the live close
    pack, cash pulse, morning brief and finance tools already read."""

    def test_balance_frozensets_unchanged(self):
        assert gf._OPENING_BALANCE_LABELS == frozenset({
            "opening balance", "beginning balance", "beginning cash/cc",
        })
        assert gf._CLOSING_BALANCE_LABELS == frozenset({
            "closing balance", "ending balance", "ending cash/cc book balance",
        })
        assert gf._PORTFOLIO_TOTAL_LABELS == frozenset({
            "portfolio total", "total portfolio", "grand total",
            "net total", "total net", "portfolio net",
        })

    def test_entity_to_tab_unchanged(self):
        assert gf.entity_to_tab("LEX-LLC") == "CF_LLC"
        assert gf.entity_to_tab("OSN") == "OSN Consolidated"
        assert gf.entity_to_tab("OSN", "partner distributions?") == "CF_OSN Core4"
        assert gf.entity_to_tab("NOPE") == "CF_SUMMARY"

    def test_forecast_overwrite_epsilon_unchanged(self):
        assert gf._FORECAST_OVERWRITE_EPSILON == 1.00
        assert gf._forecast_overwritten(100.0, 100.4) is True
        assert gf._forecast_overwritten(100.0, 110.0) is False

    def test_summary_parse_still_works_on_the_live_layout(self):
        csv_text = _tab_csv(
            ["7-24", "7-31"],
            ending=[("100", "100", "- "), ("200", "200", "- ")],
            beginning=[("40", "40", ""), ("50", "50", "")],
        )
        s = gf._parse_cashflow_csv(csv_text, "2026-08-05")
        assert s.week_label == "Week of 7-31"
        assert s.closing_balance == 200.0
        assert s.opening_balance == 50.0
        assert [w["week"] for w in s.ending_cash_dual] == ["7-24", "7-31"]

    def test_hr_llc_stays_out_of_the_entity_tab_map(self):
        """CF_HR LLC is Harrison's personal books — never an entity target."""
        assert "CF_HR LLC" not in set(gf.ENTITY_TO_TAB.values())
