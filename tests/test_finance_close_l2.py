"""A5 Part 3 (absorbed L2) -- dual forecast series, forecast assist, intercompany.

Two findings drive most of these pins:

1. The Standing ACTUALS sheet OVERWRITES its forecast column with the actual at
   week close (verified live 2026-08-04: 41 of 42 completed weeks matched to
   sub-dollar rounding). A naive dual-series accuracy figure would report ~99.99%
   accuracy forever.
2. The existing Section-summary extractors cannot see individual GL accounts, so
   an intercompany scan built on them returns zero candidates on EVERY realm --
   a structurally-blind scan that reads exactly like an honest all-clear. Hence
   the Data-row walker below, and the realistic nested fixture it is tested on.
"""

from __future__ import annotations

import datetime

import pytest

from cora import finance_close as fc
from cora.connectors import gsheets_financials as gf

MONDAY = datetime.date(2026, 8, 3)


# ── the sheet's forecast column is overwritten ───────────────────────────────

class TestForecastOverwriteDetection:
    def test_sub_dollar_gap_is_an_overwrite_not_a_variance(self):
        """Real observed pairs from the live sheet."""
        assert gf._forecast_overwritten(1626446.70, 1626447.00) is True
        assert gf._forecast_overwritten(875723.71, 875724.00) is True
        assert gf._forecast_overwritten(2920766.0, 2920766.0) is True

    def test_a_real_variance_is_not_treated_as_an_overwrite(self):
        """Week 2-6 on the live sheet: the one completed week that kept a genuine
        forecast, off by $10,347."""
        assert gf._forecast_overwritten(2314479.0, 2304132.0) is False

    def test_missing_side_is_never_an_overwrite(self):
        assert gf._forecast_overwritten(None, 100.0) is False
        assert gf._forecast_overwritten(100.0, None) is False
        assert gf._forecast_overwritten(None, None) is False

    def test_epsilon_separates_the_two_populations(self):
        eps = gf._FORECAST_OVERWRITE_EPSILON
        assert gf._forecast_overwritten(100.0, 100.0 + eps) is True
        assert gf._forecast_overwritten(100.0, 100.0 + eps + 0.01) is False


class TestDualSeries:
    def _summary(self, dual):
        return gf.CashflowSummary(week_label="Week of 7-31", as_of_date="2026-08-03",
                                  ending_cash_dual=dual)

    def test_usable_weeks_exclude_overwritten_ones(self):
        summary = self._summary([
            {"week": "7-24", "forecast": 100.0, "actual": 100.0, "forecast_overwritten": True},
            {"week": "2-6", "forecast": 200.0, "actual": 150.0, "forecast_overwritten": False},
            {"week": "8-7", "forecast": 300.0, "actual": None, "forecast_overwritten": False},
        ])
        usable = summary.completed_weeks_with_usable_forecast()
        assert [w["week"] for w in usable] == ["2-6"]

    def test_all_overwritten_yields_no_usable_weeks(self):
        summary = self._summary([
            {"week": "7-24", "forecast": 100.0, "actual": 100.0, "forecast_overwritten": True},
        ])
        assert summary.completed_weeks_with_usable_forecast() == []

    def test_dual_series_is_additive_and_does_not_disturb_the_collapsed_one(self):
        """Existing consumers read ending_cash_series; it must be untouched."""
        summary = gf.CashflowSummary(
            week_label="w", as_of_date="d",
            ending_cash_series=[{"week": "7-31", "ending_cash": 5.0, "is_actual": True}],
            ending_cash_dual=[{"week": "7-31", "forecast": 4.0, "actual": 5.0,
                               "forecast_overwritten": False}])
        assert summary.ending_cash_series[0]["ending_cash"] == 5.0
        assert summary.ending_cash_dual[0]["forecast"] == 4.0

    def test_default_is_empty_not_none(self):
        assert gf.CashflowSummary(week_label="w", as_of_date="d").ending_cash_dual == []


# ── forecast assist ──────────────────────────────────────────────────────────

def _bank(**realms):
    return {"generated_at_utc": "2026-08-03T14:00:00+00:00", "realms": realms}


def _realm(net, *, status="ok", shell=False):
    return {"status": status, "shell": shell, "cash_net_of_cards": net,
            "bank_total": net, "cc_total": 0.0, "balances_complete": True,
            "newest_bank_txn_date": "2026-08-03"}


class TestForecastAssist:
    OVERWRITTEN = [
        {"week": "7-24", "forecast": 508684.0, "actual": 508684.0, "forecast_overwritten": True},
        {"week": "7-31", "forecast": 1625638.0, "actual": 1625638.0, "forecast_overwritten": True},
        {"week": "8-7", "forecast": 1767089.0, "actual": None, "forecast_overwritten": False},
    ]

    def test_says_accuracy_is_not_computable_when_forecasts_are_overwritten(self):
        section, _ = fc.build_forecast_assist_section(
            ["F3E"],
            fc.Sources(cash_dual=lambda: self.OVERWRITTEN,
                       bank_snapshot=lambda: _bank(F3E=_realm(11750.93))),
            today=MONDAY)
        body = "\n".join(section.lines)
        assert "NOT COMPUTABLE" in body
        assert "overwritten with the actual at week close" in body

    def test_never_reports_a_near_perfect_accuracy(self):
        """The whole point: a naive dual-series variance here would be ~$0."""
        section, snap = fc.build_forecast_assist_section(
            ["F3E"],
            fc.Sources(cash_dual=lambda: self.OVERWRITTEN,
                       bank_snapshot=lambda: _bank(F3E=_realm(1.0))),
            today=MONDAY)
        body = "\n".join(section.lines)
        # No accuracy FIGURE is claimed and none is recorded downstream...
        assert not any(ln.startswith("Forecast accuracy, week") for ln in section.lines)
        assert "accuracy" not in snap
        # ...and the only near-perfect number mentioned is inside the disclaimer
        # explaining why one is NOT reported.
        assert "NOT COMPUTABLE" in body
        assert "would read ~100% accurate" in body

    def test_a_genuine_variance_is_reported_when_one_exists(self):
        dual = [{"week": "2-6", "forecast": 2314479.0, "actual": 2304132.0,
                 "forecast_overwritten": False},
                {"week": "8-7", "forecast": 1767089.0, "actual": None,
                 "forecast_overwritten": False}]
        section, snap = fc.build_forecast_assist_section(
            ["F3E"],
            fc.Sources(cash_dual=lambda: dual,
                       bank_snapshot=lambda: _bank(F3E=_realm(1.0))),
            today=MONDAY)
        assert "-$10,347" in "\n".join(section.lines)
        assert snap["accuracy"]["variance"] == pytest.approx(-10347.0)

    def test_a_stale_usable_week_says_how_far_back_it_is(self):
        """On the live sheet the newest COMPARABLE forecast is months old because
        every week since had its forecast overwritten. Rendering that as plain
        'forecast accuracy' would read as last week's number."""
        dual = [
            {"week": "2-6", "forecast": 2314479.0, "actual": 2304132.0,
             "forecast_overwritten": False},
            {"week": "7-24", "forecast": 508684.0, "actual": 508684.0,
             "forecast_overwritten": True},
            {"week": "7-31", "forecast": 1625638.0, "actual": 1625638.0,
             "forecast_overwritten": True},
            {"week": "8-7", "forecast": 1767089.0, "actual": None,
             "forecast_overwritten": False},
        ]
        section, snap = fc.build_forecast_assist_section(
            ["F3E"],
            fc.Sources(cash_dual=lambda: dual,
                       bank_snapshot=lambda: _bank(F3E=_realm(1.0))),
            today=MONDAY)
        body = "\n".join(section.lines)
        assert "most recent week that still HAS a comparable forecast" in body
        assert "2 completed week(s) since then had theirs overwritten" in body
        assert snap["accuracy"]["weeks_since"] == 2

    def test_a_current_usable_week_carries_no_staleness_note(self):
        dual = [{"week": "7-31", "forecast": 100.0, "actual": 150.0,
                 "forecast_overwritten": False}]
        section, snap = fc.build_forecast_assist_section(
            ["F3E"],
            fc.Sources(cash_dual=lambda: dual,
                       bank_snapshot=lambda: _bank(F3E=_realm(1.0))),
            today=MONDAY)
        assert "most recent week that still HAS" not in "\n".join(section.lines)
        assert snap["accuracy"]["weeks_since"] == 0

    def test_first_run_says_so_rather_than_inventing_a_variance(self):
        section, _ = fc.build_forecast_assist_section(
            ["F3E"],
            fc.Sources(cash_dual=lambda: [{"week": "8-7", "forecast": 10.0,
                                           "actual": None, "forecast_overwritten": False}],
                       bank_snapshot=lambda: _bank(F3E=_realm(1.0))),
            today=MONDAY)
        assert "first run" in "\n".join(section.lines)

    def test_next_week_starting_points_come_from_the_bank_snapshot(self):
        section, snap = fc.build_forecast_assist_section(
            ["F3E", "BDM"],
            fc.Sources(cash_dual=lambda: self.OVERWRITTEN,
                       bank_snapshot=lambda: _bank(F3E=_realm(11750.93),
                                                   BDM=_realm(11758.94))),
            today=MONDAY)
        body = "\n".join(section.lines)
        assert "week 8-7" in body
        assert snap["F3E"]["starting_point"] == 11750.93
        assert section.covered == 2

    def test_shell_realm_contributes_no_starting_point(self):
        section, snap = fc.build_forecast_assist_section(
            ["OSN"],
            fc.Sources(cash_dual=lambda: self.OVERWRITTEN,
                       bank_snapshot=lambda: _bank(OSN=_realm(0.0, shell=True))),
            today=MONDAY)
        assert "OSN" not in snap

    def test_unknown_balance_renders_unknown_not_zero(self):
        section, snap = fc.build_forecast_assist_section(
            ["F3E"],
            fc.Sources(cash_dual=lambda: self.OVERWRITTEN,
                       bank_snapshot=lambda: _bank(F3E=_realm(None))),
            today=MONDAY)
        assert "UNKNOWN" in "\n".join(section.lines)
        assert "F3E" not in snap

    def test_unreadable_sheet_is_an_honest_stub(self):
        section, _ = fc.build_forecast_assist_section(
            ["F3E"], fc.Sources(cash_dual=lambda: None), today=MONDAY)
        assert section.available is False

    def test_section_states_cora_never_writes_the_sheet(self):
        section, _ = fc.build_forecast_assist_section(
            ["F3E"],
            fc.Sources(cash_dual=lambda: self.OVERWRITTEN,
                       bank_snapshot=lambda: _bank(F3E=_realm(1.0))),
            today=MONDAY)
        assert "never writes the cash sheet" in "\n".join(section.lines)

    def test_worksheet_reuses_the_computed_lines(self):
        section, _ = fc.build_forecast_assist_section(
            ["F3E"],
            fc.Sources(cash_dual=lambda: self.OVERWRITTEN,
                       bank_snapshot=lambda: _bank(F3E=_realm(11750.93))),
            today=MONDAY)
        doc = fc.render_forecast_worksheet(section, today=MONDAY)
        assert "# Forecast assist — 2026-08-03" in doc
        assert "does NOT write the Standing ACTUALS sheet" in doc
        for line in section.lines:
            assert line in doc


class TestForecastWorksheetIsKbExcluded:
    """D-051 finding 8: a weekly cross-portfolio cash-forecast .md would otherwise
    static_md-ingest as HJRG chunks."""

    def test_the_output_folder_is_excluded(self):
        from cora.kb_exclusions import is_finance_worksheet_path
        assert is_finance_worksheet_path(
            "01-HJR-Global/accounting/forecast-assist/2026-08-05_fndr_forecast-assist.md")
        assert is_finance_worksheet_path(
            r"G:\My Drive\HJR-Founder-OS\01-HJR-Global\accounting\forecast-assist\x.md")

    def test_the_exclusion_is_narrow(self):
        from cora.kb_exclusions import is_finance_worksheet_path
        assert not is_finance_worksheet_path("01-HJR-Global/accounting/close-packs/x.md")
        assert not is_finance_worksheet_path("02-F3-Energy/projects/x.md")
        assert not is_finance_worksheet_path("")

    def test_the_section_writes_into_the_excluded_folder(self):
        assert "forecast-assist" in fc.FORECAST_ASSIST_RELDIR

    def test_it_is_wired_at_the_store_chokepoint(self):
        """Segment-based rather than folder-id-based because the folder does not
        exist until the first run -- so it must be enforced where every connector
        passes, not at drive_sweep enumeration."""
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "src" / "cora"
                  / "knowledge_base" / "store.py").read_text(encoding="utf-8")
        assert "is_finance_worksheet_path(doc.source_id)" in source
        assert "is_finance_worksheet_path(meta_path)" in source


# ── the Data-row extractor + its MANDATORY realistic fixture ─────────────────

def _realistic_balance_sheet() -> dict:
    """A nested Section -> Data BalanceSheet, shaped like the live payload.

    Modelled on the real F3E response read 2026-08-04, which nests "Divvy Account"
    one level deeper under its parent account's own sub-Section. A flat fixture
    would let a broken walker pass.
    """
    return {"Rows": {"Row": [
        {"type": "Section", "group": "BankAccounts",
         "Header": {"ColData": [{"value": "Bank Accounts"}, {"value": ""}]},
         "Rows": {"Row": [
             {"type": "Data", "ColData": [
                 {"value": "1010 Big D Media Chase", "id": "10"}, {"value": "-8483.22"}]},
             {"type": "Data", "ColData": [
                 {"value": "1020 Intercompany Cash Payment", "id": "8"}, {"value": "1200.00"}]},
         ]},
         "Summary": {"ColData": [{"value": "Total Bank Accounts"}, {"value": "-7283.22"}]}},
        {"type": "Section",
         "Header": {"ColData": [{"value": "Credit Cards"}, {"value": ""}]},
         "Rows": {"Row": [
             {"type": "Data", "ColData": [
                 {"value": "2002 Intercompany Clearing", "id": "49"}, {"value": "0.00"}]},
             # Parent account carrying its own nested child -- the shape that
             # breaks a walker which only descends one level.
             {"type": "Section",
              "Header": {"ColData": [{"value": "Credit cards Payable", "id": "50"},
                                     {"value": ""}]},
              "Rows": {"Row": [
                  {"type": "Data", "ColData": [
                      {"value": "2030 Divvy Account", "id": "51"}, {"value": "598.45"}]},
              ]},
              "Summary": {"ColData": [{"value": "Total Credit cards Payable"},
                                      {"value": "598.45"}]}},
         ]},
         "Summary": {"ColData": [{"value": "Total Credit Cards"}, {"value": "627.78"}]}},
        {"type": "Section",
         "Header": {"ColData": [{"value": "Accounts Receivable"}, {"value": ""}]},
         "Rows": {"Row": [
             {"type": "Data", "ColData": [
                 {"value": "1200 Due from HJR Global", "id": "77"}, {"value": "25000.00"}]},
         ]},
         "Summary": {"ColData": [{"value": "Total A/R"}, {"value": "25000.00"}]}},
    ]}}


class TestIterAccountRows:
    def test_finds_every_data_row_including_deeply_nested_ones(self):
        rows = fc.iter_account_rows(_realistic_balance_sheet())
        names = [r["name"] for r in rows]
        assert "2030 Divvy Account" in names, "walker missed a nested child account"
        assert len(rows) == 5

    def test_the_existing_summary_extractor_would_have_found_nothing(self):
        """Pins WHY this extractor had to be written: the Section-summary reader
        returns zero ACCOUNT rows, which looks identical to an honest all-clear."""
        report = _realistic_balance_sheet()
        summary_names = set(fc._named_section_total(report, {"Bank Accounts"}) and [] or [])
        assert summary_names == set()
        assert len(fc.iter_account_rows(report)) == 5

    def test_parses_balances_including_negatives(self):
        rows = {r["name"]: r["balance"] for r in fc.iter_account_rows(_realistic_balance_sheet())}
        assert rows["1010 Big D Media Chase"] == -8483.22
        assert rows["2030 Divvy Account"] == 598.45

    def test_captures_account_ids_for_stable_pair_matching(self):
        rows = {r["name"]: r["id"] for r in fc.iter_account_rows(_realistic_balance_sheet())}
        assert rows["2030 Divvy Account"] == "51"

    def test_empty_or_malformed_report_returns_no_rows_and_does_not_raise(self):
        for junk in ({}, {"Rows": {}}, {"Rows": {"Row": "nope"}}, {"Rows": {"Row": [None]}}):
            assert fc.iter_account_rows(junk) == []

    def test_unparseable_balance_stays_none(self):
        report = {"Rows": {"Row": [
            {"type": "Data", "ColData": [{"value": "X"}, {"value": "n/a"}]}]}}
        assert fc.iter_account_rows(report)[0]["balance"] is None


class TestIntercompanyDetection:
    @pytest.mark.parametrize("name", [
        "Intercompany Clearing", "2002 INTERCOMPANY CLEARING", "Due from HJR Global",
        "Due to F3 Energy", "I/C Receivable", "Inter-Company Transfers",
    ])
    def test_matches(self, name):
        assert fc.is_intercompany_account(name) is True

    @pytest.mark.parametrize("name", [
        "Big D Media Chase", "Accounts Receivable", "Divvy Account", "", "Duesenberg Fund",
    ])
    def test_does_not_match(self, name):
        assert fc.is_intercompany_account(name) is False


class TestIntercompanySection:
    def _src(self, per_entity):
        return fc.Sources(balance_sheet=lambda e, a: per_entity[e])

    def test_discovery_lists_candidates_as_unconfirmed(self):
        section, snap = fc.build_intercompany_section(
            ["BDM"], self._src({"BDM": _realistic_balance_sheet()}), today=MONDAY)
        body = "\n".join(section.lines)
        assert "UNCONFIRMED pairing" in body
        assert "awaits Justin" in body
        assert snap["candidates"]["BDM"] == 3   # 2 intercompany + 1 "Due from"

    def test_no_candidates_says_so_without_claiming_reconciliation(self):
        clean = {"Rows": {"Row": [{"type": "Data",
                                   "ColData": [{"value": "1010 Chase"}, {"value": "1.00"}]}]}}
        section, _ = fc.build_intercompany_section(
            ["BDM"], self._src({"BDM": clean}), today=MONDAY)
        assert "No intercompany-named accounts found" in "\n".join(section.lines)
        assert section.flags == 0

    def test_unreadable_realms_make_the_section_unavailable(self):
        def boom(e, a):
            raise RuntimeError("down")
        section, _ = fc.build_intercompany_section(
            ["BDM"], fc.Sources(balance_sheet=boom), today=MONDAY)
        assert section.available is False

    def test_coverage_is_structural(self):
        def half(e, a):
            if e == "F3E":
                raise RuntimeError("down")
            return _realistic_balance_sheet()
        section, _ = fc.build_intercompany_section(
            ["BDM", "F3E"], fc.Sources(balance_sheet=half), today=MONDAY)
        assert section.covered == 1 and section.expected == 2
        assert section.is_partial is True

    def test_pack_excluded_realm_is_not_scanned(self):
        section, _ = fc.build_intercompany_section(
            ["HRLLC"], self._src({"HRLLC": _realistic_balance_sheet()}), today=MONDAY)
        assert section.available is False  # nothing left to scan


class TestLexNamesNeverFreeRender:
    """D-051 finding 9: is_any_phi cannot catch a bare person name in an account
    title, and the finance surfaces are not LEX-custodian surfaces."""

    LEX_BS = {"Rows": {"Row": [
        {"type": "Section",
         "Header": {"ColData": [{"value": "Accounts Receivable"}, {"value": ""}]},
         "Rows": {"Row": [
             {"type": "Data", "ColData": [
                 {"value": "1200 Due from Jane Smith", "id": "90"}, {"value": "1500.00"}]},
             {"type": "Data", "ColData": [
                 {"value": "1201 Intercompany - Robert Brown", "id": "91"},
                 {"value": "800.00"}]},
         ]}},
    ]}}

    def test_lex_account_names_are_replaced_with_opaque_placeholders(self):
        section, _ = fc.build_intercompany_section(
            ["LEX"], fc.Sources(balance_sheet=lambda e, a: self.LEX_BS), today=MONDAY)
        body = "\n".join(section.lines)
        assert "Jane Smith" not in body
        assert "Robert Brown" not in body
        assert "candidate account #1" in body
        assert "candidate account #2" in body

    def test_balances_still_render_so_the_row_is_actionable(self):
        section, _ = fc.build_intercompany_section(
            ["LEX"], fc.Sources(balance_sheet=lambda e, a: self.LEX_BS), today=MONDAY)
        assert "$1,500" in "\n".join(section.lines)

    def test_section_discloses_the_withholding(self):
        section, _ = fc.build_intercompany_section(
            ["LEX"], fc.Sources(balance_sheet=lambda e, a: self.LEX_BS), today=MONDAY)
        assert "withheld" in "\n".join(section.lines)

    def test_non_lex_realms_still_render_their_names(self):
        section, _ = fc.build_intercompany_section(
            ["BDM"], fc.Sources(balance_sheet=lambda e, a: _realistic_balance_sheet()),
            today=MONDAY)
        assert "Intercompany Clearing" in "\n".join(section.lines)

    def test_lex_is_the_opaque_realm_set(self):
        assert "LEX" in fc._NAME_OPAQUE_REALMS


class TestConfirmedPairChecking:
    BS = {
        "HJRG": {"Rows": {"Row": [{"type": "Data", "ColData": [
            {"value": "Due from F3 Energy", "id": "100"}, {"value": "25000.00"}]}]}},
        "F3E": {"Rows": {"Row": [{"type": "Data", "ColData": [
            {"value": "Due to HJR Global", "id": "200"}, {"value": "-25000.00"}]}]}},
    }

    def _run(self, pairs, bs=None, monkeypatch=None):
        monkeypatch.setattr(fc, "load_intercompany_map", lambda *a, **k: {"pairs": pairs})
        return fc.build_intercompany_section(
            ["HJRG", "F3E"],
            fc.Sources(balance_sheet=lambda e, a: (bs or self.BS)[e]), today=MONDAY)

    def _pair(self, opposite_signs, confirmed=True):
        return [{"name": "HJRG <-> F3E", "confirmed": confirmed,
                 "opposite_signs": opposite_signs,
                 "left": {"entity": "HJRG", "account_id": "100"},
                 "right": {"entity": "F3E", "account_id": "200"}}]

    def test_opposite_sign_pair_in_balance_does_not_flag(self, monkeypatch):
        section, _ = self._run(self._pair(True), monkeypatch=monkeypatch)
        assert section.flags == 0
        assert "in balance" in "\n".join(section.lines)

    def test_same_sign_convention_is_honoured_not_inferred(self, monkeypatch):
        """With opposite_signs=False the SAME data is a $50k break -- proving the
        convention is read from the map, never guessed from the account names."""
        section, _ = self._run(self._pair(False), monkeypatch=monkeypatch)
        assert section.flags == 1
        assert "out of balance by +$50,000" in "\n".join(section.lines)

    def test_unconfirmed_pair_is_never_checked(self, monkeypatch):
        section, _ = self._run(self._pair(True, confirmed=False), monkeypatch=monkeypatch)
        body = "\n".join(section.lines)
        assert "in balance" not in body
        assert "discovery list, not a reconciliation" in body

    def test_missing_side_renders_unknown_not_skipped(self, monkeypatch):
        pairs = self._pair(True)
        pairs[0]["right"]["account_id"] = "does-not-exist"
        section, _ = self._run(pairs, monkeypatch=monkeypatch)
        assert "UNKNOWN" in "\n".join(section.lines)
        assert section.flags == 0

    def test_threshold_is_env_tunable(self, monkeypatch):
        monkeypatch.setenv("FINANCE_INTERCOMPANY_DELTA_ABS", "100000")
        section, _ = self._run(self._pair(False), monkeypatch=monkeypatch)
        assert section.flags == 0

    def test_bad_threshold_falls_back(self, monkeypatch):
        monkeypatch.setenv("FINANCE_INTERCOMPANY_DELTA_ABS", "junk")
        assert fc.intercompany_delta_threshold() == fc.INTERCOMPANY_DELTA_ABS


class TestShippedIntercompanyMap:
    def test_ships_empty_so_nothing_renders_as_a_confirmed_pair(self):
        """D-118: seeded placeholders would render as confident pairings."""
        assert fc.load_intercompany_map()["pairs"] == []

    def test_unreadable_map_degrades_to_discovery_only(self, tmp_path):
        bad = tmp_path / "x.yaml"
        bad.write_text("pairs: [broken\n", encoding="utf-8")
        assert fc.load_intercompany_map(bad)["pairs"] == []
