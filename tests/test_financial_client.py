"""Unit tests for tools.financial_client — behavioral contract + formatting.

All tests mock get_cashflow() and the Slack client — no network or filesystem
access (tmp_path used for throttle/audit files).
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from cora.connectors.gsheets_financials import CashflowSummary, EntityRow, GsheetsConnectorError
from cora.tools import financial_client
from cora.tools.financial_client import (
    UNKNOWN_RESPONSE,
    _fmt_currency,
    _fmt_diff,
    _entity_line,
    _format_summary_full,
    get_cashflow_text,
    get_osn_pulse_text,
    is_throttled,
    notify_gap,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def tmp_data_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(financial_client, "_repo_root", lambda: tmp_path)
    (tmp_path / "data" / "cache").mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    yield tmp_path


def _summary(
    entities: list[EntityRow] | None = None,
    week_label: str = "W21",
    as_of_date: str = "2026-05-27",
    portfolio_forecast: float | None = 50_000.0,
    portfolio_actual: float | None = 48_000.0,
    portfolio_diff: float | None = -2_000.0,
    opening_balance: float | None = None,
    closing_balance: float | None = None,
) -> CashflowSummary:
    return CashflowSummary(
        week_label=week_label,
        as_of_date=as_of_date,
        entities=entities or [],
        portfolio_forecast=portfolio_forecast,
        portfolio_actual=portfolio_actual,
        portfolio_diff=portfolio_diff,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
    )


def _row(code: str, label: str, actual: float = 10_000.0,
         forecast: float = 12_000.0, diff: float = -2_000.0) -> EntityRow:
    return EntityRow(
        entity_code=code,
        label=label,
        actual=actual,
        forecast=forecast,
        diff=diff,
    )


# ── _fmt_currency ─────────────────────────────────────────────────────────────

class TestFmtCurrency:
    def test_positive(self):
        assert _fmt_currency(5000) == "$5,000"

    def test_negative(self):
        assert _fmt_currency(-1234) == "-$1,234"

    def test_none(self):
        assert _fmt_currency(None) == "-"

    def test_zero(self):
        assert _fmt_currency(0) == "$0"


# ── _fmt_diff ─────────────────────────────────────────────────────────────────

class TestFmtDiff:
    def test_positive_prefixed_with_plus(self):
        result = _fmt_diff(1000)
        assert result.startswith("+")

    def test_negative_not_prefixed_with_plus(self):
        result = _fmt_diff(-500)
        assert not result.startswith("+")

    def test_none(self):
        assert _fmt_diff(None) == "-"

    def test_zero_prefixed_with_plus(self):
        result = _fmt_diff(0)
        assert result.startswith("+")


# ── _entity_line ──────────────────────────────────────────────────────────────

class TestEntityLine:
    def test_includes_entity_code(self):
        row = _row("F3E", "F3 Energy")
        line = _entity_line(row)
        assert "F3E" in line

    def test_includes_actual(self):
        row = _row("F3E", "F3 Energy", actual=10_000)
        line = _entity_line(row)
        assert "10,000" in line

    def test_none_fields_omitted(self):
        row = EntityRow(entity_code="F3E", label="F3 Energy",
                        actual=None, forecast=None, diff=None)
        line = _entity_line(row)
        assert "actual" not in line
        assert "forecast" not in line
        assert "diff" not in line


# ── _format_summary_full ──────────────────────────────────────────────────────

class TestFormatSummaryFull:
    def test_portfolio_header(self):
        result = _format_summary_full(_summary())
        assert "Cash Flow" in result

    def test_entity_label_in_header(self):
        result = _format_summary_full(_summary(), entity_label="OSN")
        assert "OSN" in result

    def test_entity_filter_matches(self):
        rows = [_row("F3E", "F3 Energy"), _row("OSN", "One Stop")]
        result = _format_summary_full(_summary(entities=rows), entity_filter="F3E")
        assert "F3E" in result
        assert "OSN" not in result

    def test_entity_filter_not_found(self):
        result = _format_summary_full(_summary(), entity_filter="MISSING")
        assert "No cash flow data found" in result

    def test_portfolio_totals_present(self):
        result = _format_summary_full(_summary(portfolio_actual=48_000, portfolio_forecast=50_000))
        assert "48,000" in result
        assert "50,000" in result

    def test_opening_closing_balance(self):
        result = _format_summary_full(
            _summary(opening_balance=100_000, closing_balance=95_000)
        )
        assert "100,000" in result
        assert "95,000" in result


# ── get_cashflow_text ─────────────────────────────────────────────────────────

class TestGetCashflowText:
    @patch("cora.tools.financial_client.get_cashflow")
    def test_happy_path_returns_formatted_string(self, mock_get):
        mock_get.return_value = _summary(entities=[_row("F3E", "F3 Energy")])
        result = get_cashflow_text(entity_filter="F3E", channel="C123", user="U123")
        assert isinstance(result, str)
        assert len(result) > 10

    @patch("cora.tools.financial_client.get_cashflow")
    def test_source_opacity_no_sheet_names(self, mock_get):
        mock_get.return_value = _summary()
        result = get_cashflow_text(entity_filter="F3E")
        assert "file_id" not in result.lower()
        assert "gsheets" not in result.lower()

    @patch("cora.tools.financial_client.get_cashflow",
           side_effect=GsheetsConnectorError("no sheet"))
    def test_connector_error_returns_unknown(self, _):
        result = get_cashflow_text(entity_filter="F3E")
        assert result == UNKNOWN_RESPONSE

    @patch("cora.tools.financial_client.get_cashflow",
           side_effect=RuntimeError("unexpected"))
    def test_unexpected_error_returns_unknown(self, _):
        result = get_cashflow_text(entity_filter="F3E")
        assert result == UNKNOWN_RESPONSE

    @patch("cora.tools.financial_client.get_cashflow")
    def test_audit_log_written_on_success(self, mock_get, tmp_path):
        mock_get.return_value = _summary()
        get_cashflow_text(entity_filter="F3E", channel="C1", user="U1")
        log_path = tmp_path / "logs" / "cora-finance-queries.jsonl"
        assert log_path.exists()
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l]
        assert any(r["result_type"] == "success" for r in records)

    @patch("cora.tools.financial_client.get_cashflow",
           side_effect=GsheetsConnectorError("no sheet"))
    def test_audit_log_written_on_error(self, _, tmp_path):
        get_cashflow_text(entity_filter="F3E", channel="C1", user="U1")
        log_path = tmp_path / "logs" / "cora-finance-queries.jsonl"
        assert log_path.exists()
        records = [json.loads(l) for l in log_path.read_text().splitlines() if l]
        assert any(r["result_type"] == "connector_error" for r in records)


# ── get_osn_pulse_text ────────────────────────────────────────────────────────

class TestGetOsnPulseText:
    def _osn_summary(self):
        rows = [
            _row("OSN-GW", "Gilbert Warner", actual=8_000, forecast=9_000, diff=-1_000),
            _row("OSN-MK", "Gilbert McKellips", actual=7_500, forecast=8_000, diff=-500),
        ]
        s = _summary(entities=rows)
        s_with_osn = MagicMock()
        s_with_osn.week_label = s.week_label
        s_with_osn.as_of_date = s.as_of_date
        s_with_osn.portfolio_forecast = s.portfolio_forecast
        s_with_osn.portfolio_actual = s.portfolio_actual
        s_with_osn.portfolio_diff = s.portfolio_diff
        s_with_osn.osn_entities.return_value = rows
        return s_with_osn

    @patch("cora.tools.financial_client.get_cashflow")
    def test_happy_path_returns_string(self, mock_get):
        mock_get.return_value = self._osn_summary()
        result = get_osn_pulse_text(channel="C1", user="U1")
        assert isinstance(result, str)
        assert "OSN" in result

    @patch("cora.tools.financial_client.get_cashflow")
    def test_store_rows_in_output(self, mock_get):
        mock_get.return_value = self._osn_summary()
        result = get_osn_pulse_text()
        assert "8,000" in result or "7,500" in result

    @patch("cora.tools.financial_client.get_cashflow",
           side_effect=GsheetsConnectorError("sheet gone"))
    def test_connector_error_returns_unknown(self, _):
        assert get_osn_pulse_text() == UNKNOWN_RESPONSE

    @patch("cora.tools.financial_client.get_cashflow",
           side_effect=RuntimeError("boom"))
    def test_unexpected_error_returns_unknown(self, _):
        assert get_osn_pulse_text() == UNKNOWN_RESPONSE


# ── notify_gap / throttle ─────────────────────────────────────────────────────

class TestNotifyGap:
    @patch("cora.tools.financial_client._slack_client")
    def test_returns_unknown_response(self, mock_slack):
        mock_slack.return_value = MagicMock()
        result = notify_gap("cashflow F3E", channel="C1", user="U1")
        assert result == UNKNOWN_RESPONSE

    @patch("cora.tools.financial_client._slack_client")
    def test_posts_to_slack_first_time(self, mock_slack):
        client = MagicMock()
        mock_slack.return_value = client
        notify_gap("cashflow F3E", channel="C1", user="U1")
        client.chat_postMessage.assert_called_once()

    @patch("cora.tools.financial_client._slack_client")
    def test_throttled_does_not_post_twice(self, mock_slack):
        client = MagicMock()
        mock_slack.return_value = client
        notify_gap("balance sheet OSN", channel="C1", user="U1")
        notify_gap("balance sheet OSN", channel="C1", user="U1")
        assert client.chat_postMessage.call_count == 1

    def test_is_throttled_false_before_first_call(self):
        assert is_throttled("a unique topic no one sent yet 99999") is False

    @patch("cora.tools.financial_client._slack_client")
    def test_is_throttled_true_after_notification(self, mock_slack):
        mock_slack.return_value = MagicMock()
        topic = "unique-throttle-test-topic-xyz"
        notify_gap(topic)
        assert is_throttled(topic) is True

    @patch("cora.tools.financial_client._slack_client")
    def test_slack_api_error_does_not_raise(self, mock_slack):
        from slack_sdk.errors import SlackApiError
        client = MagicMock()
        client.chat_postMessage.side_effect = SlackApiError("err", {"error": "channel_not_found"})
        mock_slack.return_value = client
        # Should not raise — just logs warning
        result = notify_gap("cashflow topic for slack error test")
        assert result == UNKNOWN_RESPONSE
