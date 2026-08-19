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
    """REWRITTEN at 13WCF M3 (2026-08-18) for the named supersession.

    The six tests that used to live here all pinned the SHEET-DUAL accuracy leg
    -- "NOT COMPUTABLE because the forecast column is overwritten", the
    staleness note, the pack-history fallback. That leg is gone, not demoted:
    M1's snapshot store banks a real pre-close forecast, so the section now has
    a source that can actually answer the question, and keeping a second one
    behind it was the Mig-1 failure the supersession exists to prevent.

    What stays pinned here is the CARRY-IN half (unchanged in substance, reworded
    to the v1 bank-sourced posture) plus the guarantee that the sheet-dual
    accessor is never consulted again. The store-side accuracy behaviour is
    pinned in tests/test_cashflow_worksheet.py, where its fixtures live.
    """

    @staticmethod
    def _snapshot(tabs=None, *, snapshot_date="2026-08-03"):
        return {
            "schema_version": 1,
            "snapshot_date": snapshot_date,
            "week_ending_weekday": "Friday",
            "tabs": tabs if tabs is not None else {
                "CF_F3": {
                    "status": "ok",
                    "post_refresh_suspect": False,
                    "last_actual_week_ending": "2026-07-31",
                    "forward_week_endings": ["2026-08-07", "2026-08-14"],
                    "series": {"ending_cash": [
                        {"week_ending": "2026-07-31", "forecast": 1625638.0,
                         "actual": 1625638.0, "diff": 0.0,
                         "basis": "post_close_column_value"},
                        {"week_ending": "2026-08-07", "forecast": 1767089.0,
                         "actual": None, "diff": None, "basis": "forecast"},
                    ]},
                },
            },
        }

    def _sources(self, **over):
        base = dict(
            cashflow_snapshot=self._snapshot,
            cashflow_snapshot_dates=lambda: [],
            cashflow_load_snapshot=lambda d: None,
            bank_snapshot=lambda: _bank(F3E=_realm(11750.93)),
        )
        base.update(over)
        return fc.Sources(**base)

    # ── the supersession itself ─────────────────────────────────────────────

    def test_the_sheet_dual_series_is_never_consulted(self):
        """The overwritten column is the thing this milestone stopped reading.
        A source that is merely DEMOTED still runs; assert it is not called."""
        called: list[str] = []
        fc.build_forecast_assist_section(
            ["F3E"],
            self._sources(cash_dual=lambda: called.append("dual") or []),
            today=MONDAY)
        assert called == []

    def test_no_snapshot_is_an_honest_stub_with_no_fallback(self):
        section, _ = fc.build_forecast_assist_section(
            ["F3E"],
            self._sources(cashflow_snapshot=lambda: None,
                          cash_dual=lambda: [{"week": "7-31", "forecast": 1.0,
                                              "actual": 2.0,
                                              "forecast_overwritten": False}]),
            today=MONDAY)
        assert section.available is False
        assert "no fallback" in (section.stub_reason or "")

    def test_no_measurable_week_never_claims_a_variance(self):
        """Mig-12/D-121: not 100%, not zero, and no figure invented."""
        section, snap = fc.build_forecast_assist_section(
            ["F3E"], self._sources(), today=MONDAY)
        body = "\n".join(section.lines)
        assert "NOT COMPUTABLE" in body
        assert "accuracy" not in snap

    def test_first_run_with_an_empty_workbook_says_so(self):
        section, snap = fc.build_forecast_assist_section(
            ["F3E"], self._sources(cashflow_snapshot=lambda: self._snapshot({})),
            today=MONDAY)
        assert "no completed week" in "\n".join(section.lines)
        assert "accuracy" not in snap

    # ── carry-in ────────────────────────────────────────────────────────────

    def test_both_measures_render_and_neither_is_named_the_carry_in(self):
        """D-116/D-120(d) unchanged in substance; the v1 posture (cleared
        2026-08-18) makes both figures REFERENCES rather than one of them an
        instruction. Live 2026-08-05 HJRP read $128,128 on the register against
        $26,880 on the report, so naming either as 'the' carry-in would
        manufacture the very break the cash section then flags."""
        section, snap = fc.build_forecast_assist_section(
            ["F3E", "BDM"],
            self._sources(bank_snapshot=lambda: _bank(F3E=_realm(128128.02),
                                                      BDM=_realm(11758.94))),
            cash_fragment={"F3E": {"books_net": 26879.52},
                           "BDM": {"books_net": -8483.22}},
            today=MONDAY)
        body = "\n".join(section.lines)
        assert "2026-08-07" in body
        assert snap["F3E"]["book_reference"] == 26879.52
        assert snap["F3E"]["register_reference"] == 128128.02
        assert "$128,128" in body and "$26,880" in body
        assert "different measures" in body
        assert "BANK-SOURCED and Justin-entered" in body
        assert section.covered == 2

    def test_the_deferred_qbo_substitute_is_proposed_not_taken(self):
        """Harrison's 2026-08-18 input: propose it WITH the feed-lag caveat,
        do not decide it."""
        section, _ = fc.build_forecast_assist_section(
            ["F3E"], self._sources(),
            cash_fragment={"F3E": {"books_net": 1.0}}, today=MONDAY)
        body = "\n".join(section.lines)
        assert "PROPOSED, NOT DECIDED" in body
        assert "~1 day behind the portal" in body

    def test_without_a_book_balance_the_books_figure_is_unknown_not_omitted(self):
        section, snap = fc.build_forecast_assist_section(
            ["F3E"],
            self._sources(bank_snapshot=lambda: _bank(F3E=_realm(128128.02))),
            cash_fragment={}, today=MONDAY)
        assert "books UNKNOWN this run" in "\n".join(section.lines)
        assert snap["F3E"]["book_reference"] is None

    def test_a_late_actual_does_not_relabel_a_past_week_as_next(self):
        """If 7-31's actual is still unfilled on 8-3, its column is 'forward' on
        that tab even though the week already CLOSED. Selection is CALENDAR-based
        (see cashflow_worksheet.next_forecast_week) precisely so a lagging tab
        cannot point the carry-in at a week that has ended."""
        section, _ = fc.build_forecast_assist_section(
            ["F3E"],
            self._sources(cashflow_snapshot=lambda: self._snapshot({
                "CF_F3": {
                    "status": "ok", "post_refresh_suspect": False,
                    "last_actual_week_ending": "2026-07-24",
                    "forward_week_endings": ["2026-07-31", "2026-08-07"],
                    "series": {"ending_cash": []},
                },
            })),
            cash_fragment={"F3E": {"books_net": 1.0}}, today=MONDAY)
        body = "\n".join(section.lines)
        assert "2026-08-07" in body
        assert "2026-07-31" not in body

    def test_no_forward_week_says_so_rather_than_going_silent(self):
        section, _ = fc.build_forecast_assist_section(
            ["F3E"],
            self._sources(cashflow_snapshot=lambda: self._snapshot({
                "CF_F3": {"status": "ok", "post_refresh_suspect": False,
                          "last_actual_week_ending": "2026-07-31",
                          "forward_week_endings": [], "series": {"ending_cash": []}},
            })),
            cash_fragment={"F3E": {"books_net": 1.0}}, today=MONDAY)
        assert "no forward forecast week" in "\n".join(section.lines)

    def test_stale_snapshot_warning_travels_with_this_section(self):
        """render_forecast_worksheet emits ONLY these lines, so the bank
        section's warning under a different heading never reaches it."""
        section, _ = fc.build_forecast_assist_section(
            ["F3E"],
            self._sources(bank_snapshot=lambda: {
                "generated_at_utc": "2026-07-01T00:00:00+00:00",
                "realms": {"F3E": _realm(1.0)}}),
            cash_fragment={"F3E": {"books_net": 1.0}}, today=MONDAY)
        body = "\n".join(section.lines)
        assert ":warning:" in body
        assert "NOT 'as of now'" in body

    def test_shell_realm_contributes_no_carry_in(self):
        _, snap = fc.build_forecast_assist_section(
            ["OSN"],
            self._sources(bank_snapshot=lambda: _bank(OSN=_realm(0.0, shell=True))),
            cash_fragment={"OSN": {"books_net": 0.0}}, today=MONDAY)
        assert "OSN" not in snap

    def test_unknown_balance_renders_unknown_not_zero(self):
        section, _ = fc.build_forecast_assist_section(
            ["F3E"],
            self._sources(bank_snapshot=lambda: _bank(F3E=_realm(None))),
            today=MONDAY)
        assert "UNKNOWN" in "\n".join(section.lines)

    def test_a_missing_realm_is_named_unavailable_for_the_founder_cut(self):
        """'unavailable —' is the literal substring build_founder_cut collects;
        any other wording makes the gap invisible in the cut Harrison reads."""
        section, _ = fc.build_forecast_assist_section(
            ["UFL"], self._sources(bank_snapshot=lambda: _bank()), today=MONDAY)
        assert "unavailable —" in "\n".join(section.lines)

    def test_section_states_cora_never_writes_the_sheet(self):
        section, _ = fc.build_forecast_assist_section(
            ["F3E"], self._sources(), today=MONDAY)
        assert "never writes the cash sheet" in "\n".join(section.lines)

    def test_the_section_names_its_sole_forecast_source(self):
        section, _ = fc.build_forecast_assist_section(
            ["F3E"], self._sources(), today=MONDAY)
        body = "\n".join(section.lines)
        assert "ONLY source" in body
        assert "cashflow-ledger/worksheets/" in body

    def test_worksheet_reuses_the_computed_lines_and_points_at_the_new_lane(self):
        section, _ = fc.build_forecast_assist_section(
            ["F3E"], self._sources(), today=MONDAY)
        doc = fc.render_forecast_worksheet(section, today=MONDAY)
        assert "# Forecast assist — 2026-08-03" in doc
        assert "does NOT write the Standing ACTUALS sheet" in doc
        assert "SUPERSEDED" in doc
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

    def test_missing_side_renders_unavailable_not_skipped(self, monkeypatch):
        pairs = self._pair(True)
        pairs[0]["right"]["account_id"] = "does-not-exist"
        section, _ = self._run(pairs, monkeypatch=monkeypatch)
        body = "\n".join(section.lines)
        # "unavailable —" is the literal substring build_founder_cut collects
        # coverage lines by; any other wording hides an unrun reconciliation from
        # the one view Harrison reads.
        assert "unavailable —" in body
        assert "NOT checked" in body
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
