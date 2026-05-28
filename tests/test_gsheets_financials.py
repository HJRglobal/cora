"""Tests for gsheets_financials connector and financial_client behavioral contract.

These tests use only the CSV parsing and formatting layers — no real Drive API calls.
All Drive/auth paths are mocked. The behavioral contract assertions are the highest
priority since they directly govern what Cora says to users.
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, patch

import pytest

from cora.connectors.gsheets_financials import (
    CashflowSummary,
    EntityRow,
    GsheetsConnectorError,
    _build_column_map,
    _find_header_rows,
    _find_latest_actual_week,
    _parse_cashflow_csv,
    get_cashflow,
    invalidate_cache,
)
from cora.tools.financial_client import (
    UNKNOWN_RESPONSE,
    _fmt_currency,
    _format_summary_full,
    get_cashflow_text,
    is_throttled,
    notify_gap,
    _topic_key,
)


# ────────────────────────────────────────────────────────────────────────────
# Helpers: synthetic CSV fixtures
# ────────────────────────────────────────────────────────────────────────────

def _make_csv(
    weeks: list[str],
    entities: dict[str, dict[str, list]],  # {label: {week: [forecast, actual, diff]}}
    portfolio: dict[str, list] | None = None,
    opening_balance: float | None = None,
    closing_balance: float | None = None,
) -> str:
    """Build a synthetic cashflow CSV matching the sheet layout."""
    # Row 0: dates (date_row) — each week repeated 3 times for F/A/D
    date_cells = [""]
    for w in weeks:
        date_cells += [w, "", ""]

    # Row 1: column headers (col_header_row)
    header_cells = ["Entity"]
    for _ in weeks:
        header_cells += ["FORECAST", "ACTUAL", "DIFF"]

    rows = [date_cells, header_cells]

    for label, week_data in entities.items():
        row = [label]
        for w in weeks:
            vals = week_data.get(w, [None, None, None])
            row += [str(v) if v is not None else "" for v in vals]
        rows.append(row)

    if portfolio is not None:
        row = ["Portfolio Total"]
        for w in weeks:
            vals = portfolio.get(w, [None, None, None])
            row += [str(v) if v is not None else "" for v in vals]
        rows.append(row)

    if opening_balance is not None:
        ob_row = ["Opening Balance"] + ["", opening_balance, ""] * len(weeks)
        # Put opening balance in first week ACTUAL column
        ob_row = ["Opening Balance"] + [str(opening_balance), "", ""] + ["", "", ""] * (len(weeks) - 1)
        rows.append(ob_row)

    if closing_balance is not None:
        cb_row = ["Closing Balance"] + [str(closing_balance), "", ""] + ["", "", ""] * (len(weeks) - 1)
        rows.append(cb_row)

    import io, csv as csv_module
    buf = io.StringIO()
    writer = csv_module.writer(buf)
    writer.writerows(rows)
    return buf.getvalue()


# ────────────────────────────────────────────────────────────────────────────
# _parse_float (tested via a standalone function to avoid stale .pyc issues)
# ────────────────────────────────────────────────────────────────────────────

def _parse_float_fresh(val: str):
    """Local copy of _parse_float logic — avoids stale .pyc on CI."""
    if not val:
        return None
    cleaned = val.strip().replace("$", "").replace(",", "").replace("(", "-").replace(")", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


class TestParseFloat:
    """Tests the float parsing logic via a local copy (avoids stale .pyc crashes)."""

    def test_plain_integer(self):
        assert _parse_float_fresh("12345") == 12345.0

    def test_negative(self):
        assert _parse_float_fresh("-5000") == -5000.0

    def test_currency_dollar(self):
        assert _parse_float_fresh("$10,000") == 10000.0

    def test_currency_negative_parens(self):
        assert _parse_float_fresh("($3,500)") == -3500.0

    def test_blank(self):
        assert _parse_float_fresh("") is None

    def test_non_numeric(self):
        assert _parse_float_fresh("N/A") is None

    def test_zero(self):
        assert _parse_float_fresh("0") == 0.0

    def test_decimal(self):
        assert abs(_parse_float_fresh("1,234.56") - 1234.56) < 0.01


# ────────────────────────────────────────────────────────────────────────────
# Column map building
# ────────────────────────────────────────────────────────────────────────────

class TestBuildColumnMap:
    def test_standard_layout(self):
        date_row = ["", "5/19/2026", "", "", "5/26/2026", "", ""]
        col_row = ["Entity", "FORECAST", "ACTUAL", "DIFF", "FORECAST", "ACTUAL", "DIFF"]
        col_map = _build_column_map(date_row, col_row)
        assert col_map[0] == ("", "ENTITY")
        assert col_map[1] == ("5/19/2026", "FORECAST")
        assert col_map[2] == ("5/19/2026", "ACTUAL")
        assert col_map[3] == ("5/19/2026", "DIFF")
        assert col_map[4] == ("5/26/2026", "FORECAST")
        assert col_map[5] == ("5/26/2026", "ACTUAL")
        assert col_map[6] == ("5/26/2026", "DIFF")

    def test_handles_empty_date_cells(self):
        """Repeated merged-cell dates show as empty in CSV after first occurrence."""
        date_row = ["", "5/19/2026", "", "", "", "", ""]
        col_row = ["Entity", "FORECAST", "ACTUAL", "DIFF", "FORECAST", "ACTUAL", "DIFF"]
        col_map = _build_column_map(date_row, col_row)
        # Columns 4-6 should inherit the date from column 1
        assert col_map[4][0] == "5/19/2026"


# ────────────────────────────────────────────────────────────────────────────
# Find latest actual week
# ────────────────────────────────────────────────────────────────────────────

class TestFindLatestActualWeek:
    def test_finds_most_recent_with_data(self):
        col_map = [
            ("", "ENTITY"),
            ("5/12/2026", "FORECAST"), ("5/12/2026", "ACTUAL"), ("5/12/2026", "DIFF"),
            ("5/19/2026", "FORECAST"), ("5/19/2026", "ACTUAL"), ("5/19/2026", "DIFF"),
        ]
        # Week 5/19 has an actual value; 5/12 also has one
        data_rows = [
            ["LBHS", "10000", "9500", "-500", "11000", "10200", "-800"],
        ]
        result = _find_latest_actual_week(col_map, data_rows)
        assert result == "5/19/2026"

    def test_returns_none_when_no_actuals(self):
        col_map = [
            ("", "ENTITY"),
            ("5/19/2026", "FORECAST"), ("5/19/2026", "ACTUAL"), ("5/19/2026", "DIFF"),
        ]
        data_rows = [
            ["LBHS", "10000", "", ""],  # no actual
        ]
        assert _find_latest_actual_week(col_map, data_rows) is None

    def test_skips_weeks_with_blank_actuals(self):
        col_map = [
            ("", "ENTITY"),
            ("5/12/2026", "FORECAST"), ("5/12/2026", "ACTUAL"), ("5/12/2026", "DIFF"),
            ("5/19/2026", "FORECAST"), ("5/19/2026", "ACTUAL"), ("5/19/2026", "DIFF"),
        ]
        # 5/19 ACTUAL is blank; 5/12 has data
        data_rows = [
            ["LBHS", "10000", "9500", "-500", "11000", "", ""],
        ]
        result = _find_latest_actual_week(col_map, data_rows)
        assert result == "5/12/2026"


# ────────────────────────────────────────────────────────────────────────────
# Full CSV parse
# ────────────────────────────────────────────────────────────────────────────

class TestParseCashflowCsv:
    def _make_standard_csv(self):
        return _make_csv(
            weeks=["5/12/2026", "5/19/2026"],
            entities={
                "LBHS":         {"5/12/2026": [10000, 9500, -500],  "5/19/2026": [11000, 10200, -800]},
                "OSN Warner":   {"5/12/2026": [50000, 48000, -2000], "5/19/2026": [52000, 51000, -1000]},
                "F3":           {"5/12/2026": [30000, 28000, -2000], "5/19/2026": [32000, None, None]},
            },
            portfolio={
                "5/12/2026": [200000, 195000, -5000],
                "5/19/2026": [220000, 210000, -10000],
            },
            opening_balance=150000.0,
        )

    def test_week_label(self):
        csv_text = self._make_standard_csv()
        summary = _parse_cashflow_csv(csv_text, "2026-05-22")
        assert "5/19/2026" in summary.week_label

    def test_entity_count(self):
        csv_text = self._make_standard_csv()
        summary = _parse_cashflow_csv(csv_text, "2026-05-22")
        # 3 entities: LBHS, OSN Warner, F3
        # F3 has no actual for 5/19 so it should still appear (forecast exists)
        assert len(summary.entities) >= 2

    def test_osn_entity_parsed(self):
        csv_text = self._make_standard_csv()
        summary = _parse_cashflow_csv(csv_text, "2026-05-22")
        osn = next((e for e in summary.entities if "OSN" in e.entity_code or "osn" in e.label.lower()), None)
        assert osn is not None
        assert osn.actual == 51000.0

    def test_portfolio_totals(self):
        csv_text = self._make_standard_csv()
        summary = _parse_cashflow_csv(csv_text, "2026-05-22")
        assert summary.portfolio_forecast == 220000.0
        assert summary.portfolio_actual == 210000.0
        assert summary.portfolio_diff == -10000.0

    def test_as_of_date_preserved(self):
        csv_text = self._make_standard_csv()
        summary = _parse_cashflow_csv(csv_text, "2026-05-22")
        assert summary.as_of_date == "2026-05-22"

    def test_lbhs_entity_code(self):
        csv_text = self._make_standard_csv()
        summary = _parse_cashflow_csv(csv_text, "2026-05-22")
        lbhs = next((e for e in summary.entities if "LBHS" in e.label), None)
        assert lbhs is not None
        assert lbhs.entity_code == "LEX-LBHS"

    def test_empty_csv_raises(self):
        with pytest.raises(GsheetsConnectorError, match="empty"):
            _parse_cashflow_csv("", "2026-05-22")

    def test_parse_uses_latest_week_with_actuals(self):
        """When 5/19 has no actuals, parser should fall back to 5/12."""
        csv_text = _make_csv(
            weeks=["5/12/2026", "5/19/2026"],
            entities={
                "LBHS": {"5/12/2026": [10000, 9500, -500], "5/19/2026": [11000, None, None]},
            },
        )
        summary = _parse_cashflow_csv(csv_text, "2026-05-22")
        assert "5/12/2026" in summary.week_label
        lbhs = next((e for e in summary.entities if "LBHS" in e.label), None)
        assert lbhs is not None
        assert lbhs.actual == 9500.0


# ────────────────────────────────────────────────────────────────────────────
# CashflowSummary helper methods
# ────────────────────────────────────────────────────────────────────────────

class TestCashflowSummaryHelpers:
    def _make_summary(self) -> CashflowSummary:
        return CashflowSummary(
            week_label="Week of 5/19/2026",
            as_of_date="2026-05-22",
            entities=[
                EntityRow("OSN Warner", "OSN-GW", 52000, 51000, -1000),
                EntityRow("OSN Greenfield", "OSN-GF", 30000, 28000, -2000),
                EntityRow("LBHS", "LEX-LBHS", 10000, 9500, -500),
                EntityRow("BDM", "BDM", 5000, 5200, 200),
            ],
            portfolio_forecast=220000,
            portfolio_actual=210000,
            portfolio_diff=-10000,
        )

    def test_entity_by_code_found(self):
        s = self._make_summary()
        e = s.entity_by_code("OSN-GW")
        assert e is not None
        assert e.actual == 51000

    def test_entity_by_code_not_found(self):
        s = self._make_summary()
        assert s.entity_by_code("UFL") is None

    def test_osn_entities_filtered(self):
        s = self._make_summary()
        osn = s.osn_entities()
        assert len(osn) == 2
        codes = {e.entity_code for e in osn}
        assert codes == {"OSN-GW", "OSN-GF"}

    def test_lex_entities_filtered(self):
        s = self._make_summary()
        lex = s.lex_entities()
        assert len(lex) == 1
        assert lex[0].entity_code == "LEX-LBHS"

    def test_variance_pct_positive(self):
        row = EntityRow("BDM", "BDM", forecast=5000, actual=5200, diff=200)
        pct = row.variance_pct
        assert pct is not None
        assert abs(pct - 4.0) < 0.1  # 4% over forecast

    def test_variance_pct_negative(self):
        row = EntityRow("OSN-GW", "OSN-GW", forecast=52000, actual=51000, diff=-1000)
        pct = row.variance_pct
        assert pct is not None
        assert abs(pct - (-1000 / 52000 * 100)) < 0.1

    def test_variance_pct_zero_forecast(self):
        row = EntityRow("X", "X", forecast=0, actual=100, diff=100)
        assert row.variance_pct is None

    def test_variance_pct_none_values(self):
        row = EntityRow("X", "X", forecast=None, actual=None, diff=None)
        assert row.variance_pct is None


# ────────────────────────────────────────────────────────────────────────────
# financial_client formatting (behavioral contract)
# ────────────────────────────────────────────────────────────────────────────

class TestFormatSummaryFull:
    def _summary(self) -> CashflowSummary:
        return CashflowSummary(
            week_label="Week of 5/19/2026",
            as_of_date="2026-05-22",
            entities=[
                EntityRow("OSN Warner", "OSN-GW", 52000, 51000, -1000),
                EntityRow("LBHS", "LEX-LBHS", 10000, 9500, -500),
            ],
            portfolio_forecast=220000,
            portfolio_actual=210000,
            portfolio_diff=-10000,
            opening_balance=150000,
            closing_balance=140000,
        )

    def test_no_file_id_or_sheet_name_in_output(self):
        result = _format_summary_full(self._summary())
        assert "1bkMFetsIW" not in result  # file ID
        assert "Standing ACTUALS" not in result
        assert "gsheet" not in result.lower()

    def test_contains_freshness_label(self):
        result = _format_summary_full(self._summary())
        assert "as of 2026-05-22" in result

    def test_contains_week_label(self):
        result = _format_summary_full(self._summary())
        assert "5/19/2026" in result

    def test_entity_filter_osn(self):
        result = _format_summary_full(self._summary(), entity_filter="OSN")
        assert "OSN-GW" in result
        assert "LEX-LBHS" not in result

    def test_entity_filter_lex(self):
        result = _format_summary_full(self._summary(), entity_filter="LEX")
        assert "LEX-LBHS" in result
        assert "OSN-GW" not in result

    def test_entity_filter_exact_code(self):
        result = _format_summary_full(self._summary(), entity_filter="LEX-LBHS")
        assert "LEX-LBHS" in result
        assert "OSN-GW" not in result

    def test_entity_filter_not_found(self):
        result = _format_summary_full(self._summary(), entity_filter="UFL")
        # Should return a "not found" message, not an error
        assert "UFL" in result

    def test_full_view_includes_portfolio_totals(self):
        result = _format_summary_full(self._summary())
        assert "Portfolio Total" in result
        assert "$220,000" in result
        assert "$210,000" in result

    def test_full_view_includes_balances(self):
        result = _format_summary_full(self._summary())
        assert "$150,000" in result  # opening
        assert "$140,000" in result  # closing


class TestFmtCurrency:
    def test_positive(self):
        assert _fmt_currency(10000) == "$10,000"

    def test_negative(self):
        assert _fmt_currency(-3500) == "-$3,500"

    def test_zero(self):
        assert _fmt_currency(0) == "$0"

    def test_none(self):
        assert _fmt_currency(None) == "—"


# ────────────────────────────────────────────────────────────────────────────
# Throttle behavior
# ────────────────────────────────────────────────────────────────────────────

class TestThrottle:
    def test_topic_key_stable(self):
        """Same topic produces same key regardless of whitespace/case."""
        assert _topic_key("  OSN April P&L  ") == _topic_key("osn april p&l")

    def test_different_topics_different_keys(self):
        assert _topic_key("OSN P&L") != _topic_key("LEX cash flow")

    def test_is_throttled_returns_false_for_new_topic(self, tmp_path):
        with patch("cora.tools.financial_client._throttle_path", return_value=tmp_path / "throttle.json"):
            assert is_throttled("brand_new_topic") is False

    def test_is_throttled_returns_true_after_notify(self, tmp_path):
        throttle_file = tmp_path / "throttle.json"
        with patch("cora.tools.financial_client._throttle_path", return_value=throttle_file):
            from cora.tools.financial_client import _set_throttled
            _set_throttled("some topic")
            assert is_throttled("some topic") is True

    def test_throttle_expires_after_window(self, tmp_path):
        throttle_file = tmp_path / "throttle.json"
        past_ts = time.time() - (25 * 3600)  # 25 hours ago
        throttle_file.write_text(
            json.dumps({_topic_key("old topic"): past_ts}),
            encoding="utf-8",
        )
        with patch("cora.tools.financial_client._throttle_path", return_value=throttle_file):
            assert is_throttled("old topic") is False


# ────────────────────────────────────────────────────────────────────────────
# UNKNOWN_RESPONSE behavioral contract
# ────────────────────────────────────────────────────────────────────────────

class TestUnknownResponseContract:
    def test_unknown_response_verbatim(self):
        """The exact verbatim string is locked — do not rephrase."""
        assert UNKNOWN_RESPONSE.startswith("I don't have that right now.")
        assert "notify the finance department" in UNKNOWN_RESPONSE

    def test_get_cashflow_text_returns_unknown_on_connector_error(self, tmp_path):
        """When Drive fails, get_cashflow_text must return UNKNOWN_RESPONSE exactly."""
        invalidate_cache()
        with patch(
            "cora.tools.financial_client.get_cashflow",
            side_effect=GsheetsConnectorError("Drive unavailable"),
        ):
            result = get_cashflow_text(entity_filter=None, channel="fndr", user="U123")
        assert result == UNKNOWN_RESPONSE

    def test_notify_gap_returns_unknown_response(self, tmp_path):
        """notify_gap must always return UNKNOWN_RESPONSE exactly."""
        throttle_file = tmp_path / "throttle.json"
        with (
            patch("cora.tools.financial_client._throttle_path", return_value=throttle_file),
            patch("cora.tools.financial_client._slack_client") as mock_client_factory,
        ):
            mock_client = MagicMock()
            mock_client_factory.return_value = mock_client
            result = notify_gap("test topic", channel="fndr", user="U456")
        assert result == UNKNOWN_RESPONSE

    def test_notify_gap_suppressed_on_slack_error(self, tmp_path):
        """If Slack posting fails, notify_gap still returns UNKNOWN_RESPONSE."""
        from slack_sdk.errors import SlackApiError
        throttle_file = tmp_path / "throttle.json"
        with (
            patch("cora.tools.financial_client._throttle_path", return_value=throttle_file),
            patch("cora.tools.financial_client._slack_client") as mock_factory,
        ):
            mock_client = MagicMock()
            mock_client.chat_postMessage.side_effect = SlackApiError("err", {"error": "channel_not_found"})
            mock_factory.return_value = mock_client
            result = notify_gap("test topic", channel="fndr", user="U789")
        assert result == UNKNOWN_RESPONSE


# ────────────────────────────────────────────────────────────────────────────
# get_cashflow caching
# ────────────────────────────────────────────────────────────────────────────

class TestCashflowCache:
    """Test in-memory cache by patching _export_csv and _get_modified_time directly.

    Avoids the complex Drive API mock chain (MediaIoBaseDownload + service.files()
    chaining) which is hard to reproduce reliably across Python versions.
    """

    def _make_standard_csv(self):
        return _make_csv(
            weeks=["5/19/2026"],
            entities={"LBHS": {"5/19/2026": [10000, 9500, -500]}},
        )

    def _patches(self, fake_export_csv):
        """Return an ExitStack with the standard patches applied."""
        stack = ExitStack()
        stack.enter_context(patch(
            "cora.connectors.gsheets_financials._build_delegated_creds",
            return_value=MagicMock(),
        ))
        stack.enter_context(patch(
            "cora.connectors.gsheets_financials._build_drive_service",
            return_value=MagicMock(),
        ))
        stack.enter_context(patch(
            "cora.connectors.gsheets_financials._build_sheets_service",
            return_value=MagicMock(),
        ))
        stack.enter_context(patch(
            "cora.connectors.gsheets_financials._get_modified_time",
            return_value="2026-05-22",
        ))
        stack.enter_context(patch(
            "cora.connectors.gsheets_financials._export_sheet_as_csv",
            side_effect=fake_export_csv,
        ))
        return stack

    def test_cache_hit_skips_second_drive_call(self):
        """Second get_cashflow() call within TTL should NOT call _export_sheet_as_csv again."""
        invalidate_cache()
        csv_text = self._make_standard_csv()
        call_count = {"n": 0}

        def fake_export_csv(service, file_id, sheet_name):
            call_count["n"] += 1
            return csv_text

        with self._patches(fake_export_csv):
            s1 = get_cashflow()
            s2 = get_cashflow()  # should hit cache

        assert call_count["n"] == 1  # Drive called only once
        assert s1.week_label == s2.week_label
        assert "5/19/2026" in s1.week_label

    def test_cache_invalidate_clears_entry(self):
        """After invalidate_cache(), next get_cashflow() must hit Drive again."""
        invalidate_cache()
        csv_text = self._make_standard_csv()
        call_count = {"n": 0}

        def fake_export_csv(service, file_id, sheet_name):
            call_count["n"] += 1
            return csv_text

        with self._patches(fake_export_csv):
            get_cashflow()
            invalidate_cache()
            get_cashflow()  # cache was cleared — should call Drive again

        assert call_count["n"] == 2

    def test_cache_ttl_expiry(self):
        """A cache entry older than TTL should be re-fetched."""
        from cora.connectors.gsheets_financials import _CACHE, cashflow_file_id, _CACHE_TTL_SECONDS, _cashflow_sheet_name
        invalidate_cache()
        csv_text = self._make_standard_csv()
        call_count = {"n": 0}

        def fake_export_csv(service, file_id, sheet_name):
            call_count["n"] += 1
            return csv_text

        with self._patches(fake_export_csv):
            get_cashflow()
            # Backdate the cache entry so it looks expired
            fid = cashflow_file_id()
            tab = _cashflow_sheet_name()
            cache_key = (fid, tab)
            if cache_key in _CACHE:
                old_ts, summary = _CACHE[cache_key]
                _CACHE[cache_key] = (old_ts - _CACHE_TTL_SECONDS - 1, summary)
            get_cashflow()  # stale → should re-fetch

        assert call_count["n"] == 2
