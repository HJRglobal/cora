"""Unit tests for cora.tools.qbo_client — pure functions only.

Covers:
  - parse_period()
  - _extract_top_level_sections()
  - _deep_link()
  - format_pnl_for_llm()
  - format_balance_sheet_for_llm()
  - format_ar_aging_for_llm()
  - format_ap_aging_for_llm()
  - format_recent_transactions_for_llm()

No HTTP calls, no token files, no QBO auth required.
"""

import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from cora.tools.qbo_client import (
    _ENTITY_PNL_BASIS,
    _deep_link,
    _extract_top_level_sections,
    _parse_money,
    _report_basis,
    entity_pnl_basis,
    extract_pnl_revenue,
    format_ap_aging_for_llm,
    format_ar_aging_for_llm,
    format_balance_sheet_for_llm,
    format_pnl_for_llm,
    format_recent_transactions_for_llm,
    parse_period,
)

# ── Shared helpers ────────────────────────────────────────────────────────────

_FIXED_TODAY = datetime.date(2025, 6, 15)
_REALM_ID = "9876"


def _make_section_row(name: str, total: str) -> dict:
    """Build a minimal QBO Section row with one Summary ColData pair."""
    return {
        "type": "Section",
        "Header": {"ColData": [{"value": name}]},
        "Summary": {"ColData": [{"value": "ignored_first"}, {"value": total}]},
    }


def _make_report(*rows) -> dict:
    """Wrap rows in a minimal QBO report envelope."""
    return {"Rows": {"Row": list(rows)}}


# ── parse_period ──────────────────────────────────────────────────────────────

class TestParsePeriod:
    """Tests for parse_period() date arithmetic."""

    def _today(self):
        return _FIXED_TODAY

    def _call(self, period):
        """Call parse_period with a fixed 'today' via patching."""
        fixed = datetime.date(2025, 6, 15)
        with patch("cora.tools.qbo_client.datetime") as mock_dt:
            mock_dt.date.today.return_value = fixed
            mock_dt.date.fromisoformat = datetime.date.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            return parse_period(period)

    def test_none_returns_last_30_days(self):
        start, end = self._call(None)
        assert end == "2025-06-15"
        assert start == "2025-05-16"

    def test_empty_string_returns_last_30_days(self):
        start, end = self._call("")
        assert end == "2025-06-15"
        assert start == "2025-05-16"

    def test_last_30_days_explicit(self):
        start, end = self._call("last_30_days")
        assert end == "2025-06-15"
        assert start == "2025-05-16"

    def test_last_90_days(self):
        start, end = self._call("last_90_days")
        assert end == "2025-06-15"
        assert start == "2025-03-17"

    def test_this_month(self):
        start, end = self._call("this_month")
        assert start == "2025-06-01"
        assert end == "2025-06-15"

    def test_last_month(self):
        # June 15 → last month = May 1 through May 31
        start, end = self._call("last_month")
        assert start == "2025-05-01"
        assert end == "2025-05-31"

    def test_ytd(self):
        start, end = self._call("ytd")
        assert start == "2025-01-01"
        assert end == "2025-06-15"

    def test_last_year(self):
        start, end = self._call("last_year")
        assert start == "2024-01-01"
        assert end == "2024-12-31"

    def test_explicit_range_standard(self):
        start, end = parse_period("2024-03-01 to 2024-03-31")
        assert start == "2024-03-01"
        assert end == "2024-03-31"

    def test_explicit_range_underscore_separator(self):
        start, end = parse_period("2024-03-01_to_2024-03-31")
        assert start == "2024-03-01"
        assert end == "2024-03-31"

    def test_unrecognized_string_falls_back_to_last_30(self):
        # Should not raise; just fallback
        start, end = parse_period("quarterly")
        today = datetime.date.today()
        assert end == today.isoformat()
        assert start == (today - datetime.timedelta(days=30)).isoformat()

    def test_case_insensitive_this_month(self):
        start, end = self._call("This_Month")
        assert start == "2025-06-01"

    def test_last_30_days_with_spaces(self):
        start, end = self._call("last 30 days")
        assert end == "2025-06-15"
        assert start == "2025-05-16"


# ── _extract_top_level_sections ───────────────────────────────────────────────

class TestExtractTopLevelSections:
    """Tests for _extract_top_level_sections() — structure-agnostic extractor."""

    def test_empty_report_returns_empty_dict(self):
        assert _extract_top_level_sections({}) == {}

    def test_report_with_no_rows_key(self):
        assert _extract_top_level_sections({"Header": {"ReportName": "P&L"}}) == {}

    def test_report_with_empty_rows(self):
        assert _extract_top_level_sections({"Rows": {"Row": []}}) == {}

    def test_non_section_rows_are_skipped(self):
        report = {"Rows": {"Row": [
            {"type": "Data", "ColData": [{"value": "Revenue"}, {"value": "100"}]},
        ]}}
        assert _extract_top_level_sections(report) == {}

    def test_section_row_extracted_correctly(self):
        report = _make_report(_make_section_row("Income", "50000.00"))
        result = _extract_top_level_sections(report)
        assert result == {"Income": "50000.00"}

    def test_multiple_sections_all_extracted(self):
        report = _make_report(
            _make_section_row("Income", "80000.00"),
            _make_section_row("Expenses", "60000.00"),
            _make_section_row("Net Income", "20000.00"),
        )
        result = _extract_top_level_sections(report)
        assert result["Income"] == "80000.00"
        assert result["Expenses"] == "60000.00"
        assert result["Net Income"] == "20000.00"

    def test_section_missing_header_is_skipped(self):
        row = {
            "type": "Section",
            # No "Header" key
            "Summary": {"ColData": [{"value": "x"}, {"value": "123"}]},
        }
        result = _extract_top_level_sections({"Rows": {"Row": [row]}})
        # Empty name → not included
        assert result == {}

    def test_section_missing_summary_is_skipped(self):
        row = {
            "type": "Section",
            "Header": {"ColData": [{"value": "Assets"}]},
            # No "Summary"
        }
        result = _extract_top_level_sections({"Rows": {"Row": [row]}})
        assert result == {}

    def test_uses_last_coldata_value_as_total(self):
        # Summary has three ColData — the last one is the total
        row = {
            "type": "Section",
            "Header": {"ColData": [{"value": "Assets"}]},
            "Summary": {"ColData": [
                {"value": ""},
                {"value": "sub-total"},
                {"value": "99999.99"},
            ]},
        }
        result = _extract_top_level_sections({"Rows": {"Row": [row]}})
        assert result["Assets"] == "99999.99"


# ── _parse_money ──────────────────────────────────────────────────────────────

class TestParseMoney:
    """Tests for _parse_money() -- QBO summary value -> float USD."""

    def test_plain_decimal(self):
        assert _parse_money("12345.67") == 12345.67

    def test_thousands_separator(self):
        assert _parse_money("12,345.67") == 12345.67

    def test_dollar_sign(self):
        assert _parse_money("$1,000.00") == 1000.0

    def test_parens_are_negative(self):
        assert _parse_money("(1,234.56)") == -1234.56

    def test_integer_string(self):
        assert _parse_money("500") == 500.0

    def test_empty_string_is_none(self):
        assert _parse_money("") is None

    def test_none_is_none(self):
        assert _parse_money(None) is None

    def test_garbage_is_none(self):
        assert _parse_money("n/a") is None

    def test_lone_parens_is_none(self):
        assert _parse_money("()") is None

    def test_zero_is_zero_not_none(self):
        # Load-bearing: 0.0 (a real zero week) must be distinct from None (no data).
        assert _parse_money("0.00") == 0.0
        assert _parse_money("0.00") is not None
        assert _parse_money("0") == 0.0


# ── extract_pnl_revenue ─────────────────────────────────────────────────────────

class TestExtractPnlRevenue:
    """Tests for extract_pnl_revenue() -- top-line revenue from a P&L report."""

    def test_income_section(self):
        report = _make_report(
            _make_section_row("Income", "50000.00"),
            _make_section_row("Expenses", "30000.00"),
            _make_section_row("Net Income", "20000.00"),
        )
        assert extract_pnl_revenue(report) == 50000.0

    def test_total_income_label(self):
        report = _make_report(_make_section_row("Total Income", "12345.00"))
        assert extract_pnl_revenue(report) == 12345.0

    def test_case_insensitive(self):
        report = _make_report(_make_section_row("INCOME", "999.00"))
        assert extract_pnl_revenue(report) == 999.0

    def test_no_income_section_returns_none(self):
        report = _make_report(_make_section_row("Expenses", "30000.00"))
        assert extract_pnl_revenue(report) is None

    def test_empty_report_returns_none(self):
        assert extract_pnl_revenue({}) is None

    def test_other_income_not_treated_as_top_line(self):
        report = _make_report(_make_section_row("Other Income", "100.00"))
        assert extract_pnl_revenue(report) is None

    def test_net_income_not_treated_as_revenue(self):
        report = _make_report(_make_section_row("Net Income", "20000.00"))
        assert extract_pnl_revenue(report) is None

    def test_income_preferred_over_other_income(self):
        report = _make_report(
            _make_section_row("Income", "80000.00"),
            _make_section_row("Other Income", "5000.00"),
        )
        assert extract_pnl_revenue(report) == 80000.0

    def test_zero_income_returns_zero_not_none(self):
        # A genuine zero-revenue week is kept (0.0), NOT dropped as "no data" (None).
        report = _make_report(_make_section_row("Income", "0.00"))
        r = extract_pnl_revenue(report)
        assert r == 0.0
        assert r is not None

    def test_parens_negative_revenue_end_to_end(self):
        report = _make_report(_make_section_row("Income", "(1,234.56)"))
        assert extract_pnl_revenue(report) == -1234.56

    def test_net_sales_income_matched_by_fallback(self):
        # No exact "Income" section -> the fallback must still match a "Net Sales
        # Income"-style revenue line (it must NOT be excluded as a "net" line).
        report = _make_report(_make_section_row("Net Sales Income", "42000.00"))
        assert extract_pnl_revenue(report) == 42000.0


class TestGetProfitLossParams:
    """get_profit_loss request-param assembly (no HTTP -- _request is patched)."""

    def test_accounting_method_threaded(self):
        import cora.tools.qbo_client as qc
        captured = {}

        def _fake(entity, path, params=None):
            captured["entity"] = entity
            captured["params"] = params
            return {"Rows": {"Row": []}}

        with patch.object(qc, "_request", side_effect=_fake):
            qc.get_profit_loss("OSNGW", "2026-06-08", "2026-06-14", accounting_method="Accrual")
        assert captured["entity"] == "OSNGW"
        assert captured["params"]["accounting_method"] == "Accrual"

    def test_accounting_method_omitted_by_default(self):
        import cora.tools.qbo_client as qc
        captured = {}

        def _fake(entity, path, params=None):
            captured["params"] = params
            return {"Rows": {"Row": []}}

        with patch.object(qc, "_request", side_effect=_fake):
            qc.get_profit_loss("OSNGW", "2026-06-08", "2026-06-14")
        assert "accounting_method" not in captured["params"]


# ── _deep_link ────────────────────────────────────────────────────────────────

class TestDeepLink:
    """Tests for _deep_link() URL construction."""

    def test_known_key_profit_and_loss(self):
        url = _deep_link("profit_and_loss", "1234")
        assert "profitandloss" in url
        assert "companyId=1234" in url

    def test_known_key_balance_sheet(self):
        url = _deep_link("balance_sheet", "5678")
        assert "balancesheet" in url
        assert "companyId=5678" in url

    def test_known_key_ar_aging(self):
        url = _deep_link("ar_aging", "9999")
        assert "agedreceivables" in url
        assert "companyId=9999" in url

    def test_known_key_ap_aging(self):
        url = _deep_link("ap_aging", "0001")
        assert "agedpayables" in url
        assert "companyId=0001" in url

    def test_known_key_transactions(self):
        url = _deep_link("transactions", "1111")
        assert "transactions" in url
        assert "companyId=1111" in url

    def test_unknown_key_falls_back_to_qbo_root(self):
        url = _deep_link("nonexistent_report", "7777")
        assert "qbo.intuit.com" in url
        assert "companyId=7777" in url

    def test_realm_id_embedded_in_url(self):
        url = _deep_link("profit_and_loss", _REALM_ID)
        assert f"companyId={_REALM_ID}" in url

    def test_known_key_url_uses_ampersand_separator(self):
        # Known URLs already have '?' so separator should be '&'
        url = _deep_link("profit_and_loss", "1234")
        assert "&companyId=" in url

    def test_unknown_key_url_uses_question_mark_separator(self):
        # Fallback URL "https://qbo.intuit.com" has no '?', so separator is '?'
        url = _deep_link("totally_unknown", "1234")
        assert "?companyId=" in url


# ── Formatter helpers ─────────────────────────────────────────────────────────

def _pnl_report():
    return {
        "Header": {"ReportName": "Profit and Loss"},
        **_make_report(
            _make_section_row("Income", "100000.00"),
            _make_section_row("Net Income", "25000.00"),
        ),
    }


def _bs_report():
    return _make_report(
        _make_section_row("Total Assets", "500000.00"),
        _make_section_row("Total Liabilities and Equity", "500000.00"),
    )


def _aging_report():
    return _make_report(
        _make_section_row("Current", "10000.00"),
        _make_section_row("1 - 30", "5000.00"),
        _make_section_row("Total", "15000.00"),
    )


# ── format_pnl_for_llm ────────────────────────────────────────────────────────

class TestFormatPnlForLlm:
    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_contains_entity_name(self, _mock):
        result = format_pnl_for_llm(_pnl_report(), "F3E", "2025-01-01", "2025-03-31")
        assert "F3E" in result

    def test_no_source_branding(self):
        # B2: source-opaque -- no 'QBO'/'intuit'/companyId/'Open in' / deep link.
        result = format_pnl_for_llm(_pnl_report(), "F3E", "2025-01-01", "2025-03-31")
        assert "QBO" not in result
        assert "intuit" not in result.lower()
        assert "companyId" not in result
        assert "Open in" not in result
        assert "Profit and Loss" in result

    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_contains_section_data(self, _mock):
        result = format_pnl_for_llm(_pnl_report(), "F3E", "2025-01-01", "2025-03-31")
        assert "Income" in result
        assert "100000.00" in result
        assert "Net Income" in result

    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_empty_report_returns_fallback_message(self, _mock):
        empty = {"Header": {"ReportName": "Profit and Loss"}, "Rows": {"Row": []}}
        result = format_pnl_for_llm(empty, "F3E", "2025-01-01", "2025-03-31")
        assert "no summary rows" in result.lower() or "open it directly" in result.lower()

    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_date_range_in_output(self, _mock):
        result = format_pnl_for_llm(_pnl_report(), "F3E", "2025-01-01", "2025-03-31")
        assert "2025-01-01" in result
        assert "2025-03-31" in result


# ── WS6: accounting-basis labelling + per-entity override ─────────────────────

def _pnl_report_with_basis(basis: str):
    rep = _pnl_report()
    rep["Header"]["ReportBasis"] = basis
    return rep


class TestReportBasisLabel:
    def test_cash_basis_labeled(self):
        out = format_pnl_for_llm(_pnl_report_with_basis("Cash"), "LEX-LLC", "2025-01-01", "2025-03-31")
        assert "[Cash basis]" in out

    def test_accrual_basis_labeled(self):
        out = format_pnl_for_llm(_pnl_report_with_basis("Accrual"), "F3E", "2025-01-01", "2025-03-31")
        assert "[Accrual basis]" in out

    def test_no_basis_field_omits_label_not_fabricated(self):
        # _pnl_report() has a Header with no ReportBasis -> never invent a basis.
        out = format_pnl_for_llm(_pnl_report(), "F3E", "2025-01-01", "2025-03-31")
        assert "basis]" not in out

    def test_basis_label_on_empty_report_fallback(self):
        empty = {"Header": {"ReportName": "Profit and Loss", "ReportBasis": "Cash"},
                 "Rows": {"Row": []}}
        out = format_pnl_for_llm(empty, "LEX-LLC", "2025-01-01", "2025-03-31")
        assert "[Cash basis]" in out

    def test_report_basis_extractor(self):
        assert _report_basis({"Header": {"ReportBasis": "Accrual"}}) == "Accrual"
        assert _report_basis({"Header": {"ReportBasis": "  Cash  "}}) == "Cash"
        assert _report_basis({"Header": {"ReportName": "P&L"}}) is None
        assert _report_basis({"Header": {"ReportBasis": ""}}) is None
        assert _report_basis({}) is None


class TestEntityPnlBasisOverride:
    def test_default_map_is_empty_no_blanket_accrual(self):
        # INVARIANT CLAMP: never blanket-Accrual. The override map ships empty;
        # Harrison/Justin populate per entity once each filed basis is confirmed.
        assert _ENTITY_PNL_BASIS == {}

    def test_unset_entity_returns_none(self):
        assert entity_pnl_basis("F3E") is None
        assert entity_pnl_basis("LEX-LLC") is None

    def test_empty_entity_returns_none(self):
        assert entity_pnl_basis("") is None

    def test_override_is_case_insensitive(self, monkeypatch):
        monkeypatch.setitem(_ENTITY_PNL_BASIS, "F3E", "Accrual")
        assert entity_pnl_basis("f3e") == "Accrual"
        assert entity_pnl_basis("F3E") == "Accrual"


# ── format_balance_sheet_for_llm ──────────────────────────────────────────────

class TestFormatBalanceSheetForLlm:
    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_contains_entity_name(self, _mock):
        result = format_balance_sheet_for_llm(_bs_report(), "HJRG", "2025-06-15")
        assert "HJRG" in result

    def test_no_source_branding(self):
        result = format_balance_sheet_for_llm(_bs_report(), "HJRG", "2025-06-15")
        assert "QBO" not in result
        assert "intuit" not in result.lower()
        assert "companyId" not in result
        assert "balancesheet" not in result.lower()
        assert "Balance Sheet" in result

    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_contains_section_totals(self, _mock):
        result = format_balance_sheet_for_llm(_bs_report(), "HJRG", "2025-06-15")
        assert "Total Assets" in result
        assert "500000.00" in result

    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_empty_report_shows_fallback_message(self, _mock):
        result = format_balance_sheet_for_llm({"Rows": {"Row": []}}, "HJRG", "2025-06-15")
        assert "no summary rows" in result.lower() or "see full report" in result.lower()

    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_as_of_date_in_output(self, _mock):
        result = format_balance_sheet_for_llm(_bs_report(), "HJRG", "2025-06-15")
        assert "2025-06-15" in result


# ── format_ar_aging_for_llm ───────────────────────────────────────────────────

class TestFormatArAgingForLlm:
    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_contains_entity_name(self, _mock):
        result = format_ar_aging_for_llm(_aging_report(), "OSN")
        assert "OSN" in result

    def test_no_source_branding(self):
        result = format_ar_aging_for_llm(_aging_report(), "OSN")
        assert "QBO" not in result
        assert "intuit" not in result.lower()
        assert "agedreceivables" not in result.lower()
        assert "companyId" not in result
        assert "AR Aging" in result

    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_contains_aging_buckets(self, _mock):
        result = format_ar_aging_for_llm(_aging_report(), "OSN")
        assert "Current" in result
        assert "10000.00" in result

    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_empty_aging_shows_fallback(self, _mock):
        result = format_ar_aging_for_llm({"Rows": {"Row": []}}, "OSN")
        assert "no aging buckets" in result.lower() or "see full report" in result.lower()


# ── format_ap_aging_for_llm ───────────────────────────────────────────────────

class TestFormatApAgingForLlm:
    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_contains_entity_name(self, _mock):
        result = format_ap_aging_for_llm(_aging_report(), "LEX")
        assert "LEX" in result

    def test_no_source_branding(self):
        result = format_ap_aging_for_llm(_aging_report(), "LEX")
        assert "QBO" not in result
        assert "intuit" not in result.lower()
        assert "agedpayables" not in result.lower()
        assert "companyId" not in result
        assert "AP Aging" in result

    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_contains_aging_buckets(self, _mock):
        result = format_ap_aging_for_llm(_aging_report(), "LEX")
        assert "1 - 30" in result
        assert "5000.00" in result

    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_empty_aging_shows_fallback(self, _mock):
        result = format_ap_aging_for_llm({"Rows": {"Row": []}}, "LEX")
        assert "no aging buckets" in result.lower() or "see full report" in result.lower()


# ── format_recent_transactions_for_llm ───────────────────────────────────────

class TestFormatRecentTransactionsForLlm:

    def _payload(self, *, invoices=None, bills=None, payments=None):
        return {
            "invoices": {"Invoice": invoices or []},
            "bills":    {"Bill": bills or []},
            "payments": {"Payment": payments or []},
        }

    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_contains_entity_name(self, _mock):
        result = format_recent_transactions_for_llm(self._payload(), "F3E", 30)
        assert "F3E" in result

    def test_no_source_branding(self):
        result = format_recent_transactions_for_llm(self._payload(), "F3E", 30)
        assert "QBO" not in result
        assert "intuit" not in result.lower()
        assert "companyId" not in result
        assert "Open in" not in result
        assert "Recent activity" in result

    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_counts_items_correctly(self, _mock):
        invoices = [{"Id": "1"}, {"Id": "2"}, {"Id": "3"}]
        payload = self._payload(invoices=invoices)
        result = format_recent_transactions_for_llm(payload, "F3E", 30)
        assert "invoices: 3" in result

    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_zero_counts_shown(self, _mock):
        result = format_recent_transactions_for_llm(self._payload(), "F3E", 30)
        assert "invoices: 0" in result
        assert "bills: 0" in result
        assert "payments: 0" in result

    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_error_section_shows_error_message(self, _mock):
        payload = {
            "invoices": {"error": "QBO auth error for entity=F3E: token expired"},
            "bills":    {"Bill": []},
            "payments": {"Payment": []},
        }
        result = format_recent_transactions_for_llm(payload, "F3E", 30)
        assert "error" in result.lower()

    @patch("cora.tools.qbo_client._realm_id", return_value=_REALM_ID)
    def test_days_parameter_in_output(self, _mock):
        result = format_recent_transactions_for_llm(self._payload(), "F3E", 90)
        assert "90" in result


class TestQboToolDescriptionsSourceOpaque:
    """B2 follow-up (adversarial review MED): the QBO tool DESCRIPTIONS must not
    advertise a clickable QBO deep link -- that primed the model to fabricate an
    'open in QuickBooks (qbo.intuit.com/...)' line that the egress boundary did not
    redact. Source-level guard so the claim can't creep back."""

    def test_no_qbo_deep_link_claim_in_tool_descriptions(self):
        src = (Path(__file__).resolve().parent.parent
               / "src" / "cora" / "tools" / "tool_dispatch.py").read_text(encoding="utf-8")
        assert "QBO deep link" not in src
        assert "clickable QBO" not in src
        assert "QBO transactions deep link" not in src
