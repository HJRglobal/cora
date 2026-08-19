"""QBO transaction-level detail (cq-42787b27d4cb + cq-e3f057668e1f) and the
BalanceSheet date-param bug (cq-157a961853c4).

VERIFY-FIRST finding pinned here so it is not re-derived: the P1 "Cora cannot
pull required detail level from QBO despite having access" was NOT an access or
permission gap. `get_profit_loss` already returned QBO's full nested tree with
every leaf account in it; `_extract_top_level_sections` -- the renderer -- kept
only top-level Summary rows, so the most Cora could ever say was
"Total Expenses: 90,000.00". Outcome (A) of the seed's own branch test.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cora.tools import qbo_client as qc
from cora.tools import tool_dispatch as td

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _section(name, rows, summary=None):
    out = {"type": "Section",
           "Header": {"ColData": [{"value": name}]},
           "Rows": {"Row": rows}}
    if summary is not None:
        out["Summary"] = {"ColData": [{"value": f"Total {name}"},
                                      {"value": summary}]}
    return out


def _data(label, value):
    return {"ColData": [{"value": label}, {"value": value}]}


PNL = {
    "Header": {"ReportBasis": "Accrual", "StartPeriod": "2026-01-01",
               "EndPeriod": "2026-03-31"},
    "Rows": {"Row": [
        _section("Income", [_data("Product Sales", "125,000.00"),
                            _data("Interest Income", "42.10")], "125,042.10"),
        _section("Cost of Goods Sold", [_data("Materials", "40,000.00")], "40,000.00"),
        _section("Expenses", [
            _data("Rent", "12,000.00"),
            _data("Advertising and Marketing", "8,500.00"),
            _section("Payroll", [_data("Wages", "30,000.00"),
                                 _data("Payroll Taxes", "2,300.00")], "32,300.00"),
        ], "52,800.00"),
    ]},
}


class TestLeafExtraction:
    def test_expense_detail_reaches_the_leaf_accounts(self):
        """The whole point: the categories under the total were always in the
        payload and were being discarded by the renderer."""
        out = qc.format_expense_detail_for_llm(PNL, "F3E", "2026-01-01", "2026-03-31")
        for account in ("Rent", "Advertising and Marketing", "Wages",
                        "Payroll Taxes", "Materials"):
            assert account in out, account

    def test_income_never_leaks_into_an_expense_answer(self):
        out = qc.format_expense_detail_for_llm(PNL, "F3E", "2026-01-01", "2026-03-31")
        assert "Product Sales" not in out
        assert "Interest Income" not in out

    def test_nested_sub_accounts_are_reached(self):
        """Payroll > Wages is two levels deep; a one-level walk misses it."""
        rows = qc.extract_leaf_rows(PNL, qc._EXPENSE_SECTION_HINTS)
        assert ("Payroll", "Wages", "30,000.00") in rows

    def test_no_hints_walks_everything(self):
        rows = qc.extract_leaf_rows(PNL)
        labels = {r[1] for r in rows}
        assert "Product Sales" in labels and "Rent" in labels

    def test_values_are_moved_never_recomputed(self):
        """D-095: no total here is computed by Cora or by the model. Parenthesized
        negatives and thousands separators survive verbatim."""
        report = {"Rows": {"Row": [_section("Expenses", [_data("Refunds", "(1,234.56)")])]}}
        out = qc.format_expense_detail_for_llm(report, "F3E", "a", "b")
        assert "(1,234.56)" in out

    def test_basis_is_labelled_from_what_qbo_rendered(self):
        assert "[Accrual basis]" in qc.format_expense_detail_for_llm(
            PNL, "F3E", "2026-01-01", "2026-03-31")
        no_basis = {"Rows": PNL["Rows"]}
        assert "basis]" not in qc.format_expense_detail_for_llm(no_basis, "F3E", "a", "b")

    def test_truncation_is_stated_with_both_counts(self):
        """A silently-cut expense list reads as a complete one -- the same class
        as a digest that drops a store from the ranking and the total at once."""
        many = _section("Expenses", [_data(f"Acct {i}", str(i)) for i in range(60)])
        out = qc.format_expense_detail_for_llm(
            {"Rows": {"Row": [many]}}, "F3E", "a", "b", limit=10)
        assert "and 50 more" in out
        assert "first 10 of 60" in out

    def test_totals_only_report_says_so_rather_than_looking_empty(self):
        summary_only = {"Rows": {"Row": [
            {"type": "Section",
             "Header": {"ColData": [{"value": "Expenses"}]},
             "Summary": {"ColData": [{"value": "Expenses"}, {"value": "9.00"}]}},
        ]}}
        out = qc.format_expense_detail_for_llm(summary_only, "F3E", "a", "b")
        assert "only rolled-up totals" in out

    def test_malformed_payload_degrades_instead_of_raising(self):
        """Intuit reshapes report payloads occasionally."""
        assert qc.extract_leaf_rows({}) == []
        assert qc.extract_leaf_rows({"Rows": {"Row": [None, "junk", {}]}}) == []


VENDOR = {
    "Header": {"ReportBasis": "Accrual"},
    "Rows": {"Row": [
        _section("Cox Communications", [_data("2026-02-03 Bill", "412.55"),
                                        _data("2026-03-03 Bill", "418.02")]),
        _section("Sprouts Farmers Market", [_data("2026-02-14 Check", "1,200.00")]),
    ]},
}


class TestVendorSpend:
    def test_lists_every_vendor_when_unfiltered(self):
        out = qc.format_vendor_spend_for_llm(VENDOR, "F3E", "a", "b")
        assert "Cox Communications" in out and "Sprouts Farmers Market" in out

    def test_filter_narrows_and_states_the_scope(self):
        """A narrow answer must never be mistakable for the full picture."""
        out = qc.format_vendor_spend_for_llm(VENDOR, "F3E", "a", "b", vendor="cox")
        assert "Cox Communications" in out
        assert "Sprouts" not in out
        assert "matching 'cox'" in out

    def test_a_filter_matching_nothing_says_so_explicitly(self):
        """An empty list would read as "this vendor had no spend" when the name
        may simply be recorded differently."""
        out = qc.format_vendor_spend_for_llm(VENDOR, "F3E", "a", "b", vendor="zzz")
        assert "No purchases matching 'zzz'" in out
        assert "different name" in out

    def test_pins_the_accounting_method_when_asked(self):
        with patch.object(qc, "_request") as req:
            req.return_value = {}
            qc.get_vendor_spend("F3E", "2026-01-01", "2026-03-31",
                                accounting_method="Accrual")
            assert req.call_args.kwargs["params"]["accounting_method"] == "Accrual"

    def test_omits_the_method_when_not_configured(self):
        """Same contract as get_profit_loss: omitted means the company default,
        which differs per realm -- never silently forced."""
        with patch.object(qc, "_request") as req:
            req.return_value = {}
            qc.get_vendor_spend("F3E", "2026-01-01", "2026-03-31")
            assert "accounting_method" not in req.call_args.kwargs["params"]

    def test_sends_an_explicit_date_range(self):
        with patch.object(qc, "_request") as req:
            req.return_value = {}
            qc.get_vendor_spend("F3E", "2026-01-01", "2026-03-31")
            params = req.call_args.kwargs["params"]
            assert params["start_date"] == "2026-01-01"
            assert params["end_date"] == "2026-03-31"


class TestBalanceSheetDateParam:
    """cq-157a961853c4 -- the param QBO silently ignored."""

    def test_balance_sheet_sends_end_date_not_as_of_date(self):
        """`as_of_date` is not a BalanceSheet parameter. QBO ignored it and fell
        back to its own default period -- proven live on all 11 realms: asking
        as_of 2026-06-30 returned 2026-07-01..2026-07-31 on 2026-08-19.
        """
        with patch.object(qc, "_request") as req:
            req.return_value = {}
            qc.get_balance_sheet("F3E", "2026-06-30")
            params = req.call_args.kwargs["params"]
            assert params["end_date"] == "2026-06-30"
            assert "as_of_date" not in params

    def test_python_signature_is_unchanged_for_callers(self):
        with patch.object(qc, "_request") as req:
            req.return_value = {}
            qc.get_balance_sheet("F3E", as_of_date="2026-06-30",
                                 accounting_method="Accrual")
            params = req.call_args.kwargs["params"]
            assert params["accounting_method"] == "Accrual"

    def test_default_is_still_today(self):
        import datetime as _dt
        with patch.object(qc, "_request") as req:
            req.return_value = {}
            qc.get_balance_sheet("F3E")
            assert req.call_args.kwargs["params"]["end_date"] == \
                _dt.date.today().isoformat()


class TestCoincidentalDefaultIsDetectable:
    """The verify must catch an ignored date param even when the echo agrees."""

    def test_a_date_macro_in_the_response_is_refused(self):
        from cora import qbo_monthly_reports as qmr
        assert qmr.report_date_macro(
            {"Header": {"DateMacro": "Last Month"}}) == "Last Month"
        assert qmr.report_date_macro(
            {"Header": {"Option": [{"Name": "DateMacro", "Value": "Last Month"}]}}
        ) == "Last Month"

    def test_no_macro_on_an_explicit_period_response(self):
        from cora import qbo_monthly_reports as qmr
        assert qmr.report_date_macro(
            {"Header": {"StartPeriod": "2026-06-01", "EndPeriod": "2026-06-30"}}) is None
        assert qmr.report_date_macro({}) is None

    def test_the_populator_refuses_on_a_macro(self):
        """EndPeriod matching is NOT proof: the scheduled run asks for the prior
        month and QBO's default IS "Last Month", so a wholly-ignored date param
        produces an identical echo every time it fires. Only a backfill ever
        sees the mismatch -- so the macro check is what survives the coincidence.
        """
        src = (_REPO_ROOT / "src" / "cora" / "qbo_monthly_reports.py").read_text(
            encoding="utf-8")
        assert "report_date_macro(report)" in src
        block = src[src.index("macro = report_date_macro(report)"):]
        block = block[:block.index("data = render_xlsx(")]
        assert "summary[\"skipped\"]" in block
        assert "continue" in block


class TestToolWiring:
    @pytest.mark.parametrize("name,timeout", [
        ("qbo_get_expense_detail", 15), ("qbo_get_vendor_spend", 20)])
    def test_registered_everywhere_a_tool_must_be(self, name, timeout):
        assert any(t["name"] == name for t in td.TOOL_DEFINITIONS)
        assert name in td._TOOL_FUNCTIONS
        assert td._TOOL_TIMEOUTS[name] == timeout
        assert name in td.VERBATIM_TABLE_TOOLS

    @pytest.mark.parametrize("name", ["qbo_get_expense_detail", "qbo_get_vendor_spend"])
    def test_offered_to_the_qbo_provisioned_entities(self, name):
        for entity in ("F3E", "OSN", "LEX", "HJRP", "BDM", "FNDR"):
            offered = {t["name"] for t in td.tools_for_entity(entity)}
            assert name in offered, f"{name} not offered in {entity}"

    @pytest.mark.parametrize("name", ["qbo_get_expense_detail", "qbo_get_vendor_spend"])
    def test_refuses_in_a_tier3_channel(self, name):
        """Same gate as every other QBO tool -- these expose MORE detail, so the
        gate is more load-bearing here, not less."""
        out = td._TOOL_FUNCTIONS[name]("U1", "F3E", {"_channel_name": "f3-athletes"})
        assert out == td._QBO_TIER1_REQUIRED

    @pytest.mark.parametrize("name", ["qbo_get_expense_detail", "qbo_get_vendor_spend"])
    def test_descriptions_keep_the_source_opacity_instruction(self, name):
        spec = next(t for t in td.TOOL_DEFINITIONS if t["name"] == name)
        desc = spec["description"]
        assert "do NOT add a QuickBooks/QBO link" in desc
        assert "TIER_3" in desc

    @pytest.mark.parametrize("name", ["qbo_get_expense_detail", "qbo_get_vendor_spend"])
    def test_descriptions_forbid_model_side_arithmetic(self, name):
        """D-095. These tools return many rows, which is exactly the shape that
        tempts a model to sum or rank them."""
        desc = next(t for t in td.TOOL_DEFINITIONS
                    if t["name"] == name)["description"].lower()
        assert "do not" in desc and ("total" in desc or "rank" in desc)

    def test_expense_detail_reuses_the_pnl_fetch(self):
        """No new endpoint and no new permission for the category half -- the
        detail was always in the P&L payload."""
        src = (_REPO_ROOT / "src" / "cora" / "tools" / "tool_dispatch.py").read_text(
            encoding="utf-8")
        block = src[src.index("def _tool_qbo_get_expense_detail"):]
        block = block[:block.index("def _tool_qbo_get_vendor_spend")]
        assert "qbo_client.get_profit_loss(" in block

    def test_vendor_spend_pins_the_basis(self):
        src = (_REPO_ROOT / "src" / "cora" / "tools" / "tool_dispatch.py").read_text(
            encoding="utf-8")
        block = src[src.index("def _tool_qbo_get_vendor_spend"):]
        block = block[:block.index("# --- Finance channel enforcement ---")]
        assert "entity_pnl_basis(target)" in block
        assert "accounting_method=basis" in block
