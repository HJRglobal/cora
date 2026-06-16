"""Tests for run_cashflow_pulse.py -- Feature #13."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import run_cashflow_pulse as pulse  # noqa: E402


# ---------------------------------------------------------------------------
# _format_currency
# ---------------------------------------------------------------------------

def test_format_currency_positive():
    assert pulse._format_currency(718931.0) == "$718,931"


def test_format_currency_negative():
    assert pulse._format_currency(-42000.0) == "-$42,000"


def test_format_currency_zero():
    assert pulse._format_currency(0.0) == "$0"


def test_format_currency_none():
    assert pulse._format_currency(None) == "--"


def test_format_currency_small():
    assert pulse._format_currency(100.0) == "$100"


# ---------------------------------------------------------------------------
# _runway_flag
# ---------------------------------------------------------------------------

def test_runway_flag_healthy():
    # Large balance, no burn -- OK
    assert pulse._runway_flag(500_000.0, 0.0, 0.0) == ":white_check_mark:"


def test_runway_flag_warning_low_runway():
    # balance = 10k, actual = -8k/week => burn = 8k => 2*burn = 16k > 10k => warning
    assert pulse._runway_flag(10_000.0, -8_000.0, 0.0) == ":warning:"


def test_runway_flag_negative_balance():
    assert pulse._runway_flag(-5_000.0, 0.0, 0.0) == ":rotating_light:"


def test_runway_flag_none_balance():
    assert pulse._runway_flag(None, None, None) == ":question:"


def test_runway_flag_no_burn():
    # positive actual means inflow, no burn warning
    assert pulse._runway_flag(50_000.0, 5_000.0, 0.0) == ":white_check_mark:"


def test_runway_flag_exactly_two_weeks():
    # balance = 2 * burn is NOT a warning (< 2*burn required to warn)
    assert pulse._runway_flag(20_000.0, -10_000.0, 0.0) == ":white_check_mark:"


def test_runway_flag_just_under_two_weeks():
    assert pulse._runway_flag(19_999.0, -10_000.0, 0.0) == ":warning:"


# ---------------------------------------------------------------------------
# _fetch_entity_data
# ---------------------------------------------------------------------------

def _make_mock_summary(closing_balance=50_000.0, week_label="Week of 6/2/2026"):
    from cora.connectors.gsheets_financials import CashflowSummary
    s = CashflowSummary(
        week_label=week_label,
        as_of_date="2026-06-03",
        closing_balance=closing_balance,
        portfolio_actual=-10_000.0,
        portfolio_forecast=-8_000.0,
    )
    return s


def test_fetch_entity_data_success():
    mock_summary = _make_mock_summary()
    with patch.object(pulse, "get_cashflow", return_value=mock_summary):
        result = pulse._fetch_entity_data("F3E")
    assert result is not None
    assert result["closing_balance"] == 50_000.0
    assert result["week_label"] == "Week of 6/2/2026"


def test_fetch_entity_data_connector_error():
    from cora.connectors.gsheets_financials import GsheetsConnectorError
    with patch.object(pulse, "get_cashflow", side_effect=GsheetsConnectorError("fail")):
        result = pulse._fetch_entity_data("F3E")
    assert result is None


def test_fetch_entity_data_unexpected_error():
    with patch.object(pulse, "get_cashflow", side_effect=RuntimeError("boom")):
        result = pulse._fetch_entity_data("OSN")
    assert result is None


# ---------------------------------------------------------------------------
# build_pulse_message
# ---------------------------------------------------------------------------

def _make_results(ok_count=5, fail_count=2):
    results = []
    for i in range(ok_count):
        results.append({
            "entity_code": f"E{i}",
            "label": f"Entity {i}",
            "ok": True,
            "closing_balance": 100_000.0 * (i + 1),
            "week_label": "Week of 6/2/2026",
            "actual": -5_000.0,
            "forecast": -5_000.0,
        })
    for i in range(fail_count):
        results.append({
            "entity_code": f"FAIL{i}",
            "label": f"Failed Entity {i}",
            "ok": False,
        })
    return results


def test_build_pulse_message_contains_header():
    results = _make_results()
    msg = pulse.build_pulse_message(results)
    assert "Cross-Entity Cash Pulse" in msg


def test_build_pulse_message_contains_week_label():
    results = _make_results()
    msg = pulse.build_pulse_message(results)
    assert "Week of 6/2/2026" in msg


def test_build_pulse_message_unavailable_entities():
    results = _make_results(ok_count=2, fail_count=3)
    msg = pulse.build_pulse_message(results)
    assert "unavailable" in msg


def test_build_pulse_message_no_sheet_names():
    results = _make_results()
    msg = pulse.build_pulse_message(results)
    assert "CF_" not in msg
    assert "spreadsheet" not in msg.lower()


def test_build_pulse_message_flagged_entity():
    results = [
        {
            "entity_code": "OSN",
            "label": "One Stop Nutrition",
            "ok": True,
            "closing_balance": 5_000.0,
            "week_label": "Week of 6/2/2026",
            "actual": -10_000.0,
            "forecast": -8_000.0,
        }
    ]
    msg = pulse.build_pulse_message(results)
    assert "One Stop Nutrition" in msg


def test_build_pulse_message_all_entities_present():
    results = []
    for code, label in pulse.PULSE_ENTITIES:
        results.append({
            "entity_code": code,
            "label": label,
            "ok": True,
            "closing_balance": 50_000.0,
            "week_label": "Week of 6/2/2026",
            "actual": 0.0,
            "forecast": 0.0,
        })
    msg = pulse.build_pulse_message(results)
    for _, label in pulse.PULSE_ENTITIES:
        assert label in msg


# ---------------------------------------------------------------------------
# run() -- integration with mocked clients
# ---------------------------------------------------------------------------

def _mock_slack_client():
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "D_HARRISON"}}
    return client


def test_run_dry_run_no_dm_sent():
    mock_summary = _make_mock_summary()
    with patch.object(pulse, "get_cashflow", return_value=mock_summary), \
         patch("slack_sdk.WebClient") as mock_wc:
        mock_wc.return_value = _mock_slack_client()
        result = pulse.run(dry_run=True)
    # dry_run=True -- conversations_open should NOT be called
    assert result["entities_fetched"] > 0
    mock_wc.return_value.conversations_open.assert_not_called()


def test_run_sends_dm_when_not_dry_run():
    mock_summary = _make_mock_summary()
    with patch.object(pulse, "get_cashflow", return_value=mock_summary), \
         patch("slack_sdk.WebClient") as mock_wc:
        client = _mock_slack_client()
        mock_wc.return_value = client
        result = pulse.run(dry_run=False)
    assert result["entities_fetched"] > 0
    client.conversations_open.assert_called_once_with(users=["U0B2RM2JYJ1"])


def test_run_all_fail_skips_dm():
    from cora.connectors.gsheets_financials import GsheetsConnectorError
    with patch.object(pulse, "get_cashflow", side_effect=GsheetsConnectorError("x")), \
         patch("slack_sdk.WebClient") as mock_wc:
        client = _mock_slack_client()
        mock_wc.return_value = client
        result = pulse.run(dry_run=False)
    assert result["entities_fetched"] == 0
    client.conversations_open.assert_not_called()


def test_run_no_token_returns_early(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    result = pulse.run(dry_run=False)
    assert result == {"entities_fetched": 0, "entities_failed": 0, "flagged": 0}


def test_run_returns_correct_counts():
    mock_summary = _make_mock_summary(closing_balance=1_000_000.0)
    with patch.object(pulse, "get_cashflow", return_value=mock_summary), \
         patch("slack_sdk.WebClient") as mock_wc:
        mock_wc.return_value = _mock_slack_client()
        result = pulse.run(dry_run=True)
    assert result["entities_fetched"] == len(pulse.PULSE_ENTITIES)
    assert result["entities_failed"] == 0


def test_run_flagged_count():
    # Low balance entities should be flagged
    from cora.connectors.gsheets_financials import CashflowSummary
    low_balance_summary = CashflowSummary(
        week_label="Week of 6/2/2026",
        as_of_date="2026-06-03",
        closing_balance=5_000.0,
        portfolio_actual=-50_000.0,
        portfolio_forecast=-40_000.0,
    )
    with patch.object(pulse, "get_cashflow", return_value=low_balance_summary), \
         patch("slack_sdk.WebClient") as mock_wc:
        mock_wc.return_value = _mock_slack_client()
        result = pulse.run(dry_run=True)
    assert result["flagged"] == len(pulse.PULSE_ENTITIES)


# ---------------------------------------------------------------------------
# _pulse_enabled -- daily push disabled by default (2026-06-16, gate G-E / N1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "on", " true "])
def test_pulse_enabled_true_values(monkeypatch, value):
    monkeypatch.setenv("CASH_PULSE_ENABLED", value)
    assert pulse._pulse_enabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "disabled", "  "])
def test_pulse_enabled_false_values(monkeypatch, value):
    monkeypatch.setenv("CASH_PULSE_ENABLED", value)
    assert pulse._pulse_enabled() is False


def test_pulse_enabled_unset(monkeypatch):
    monkeypatch.delenv("CASH_PULSE_ENABLED", raising=False)
    assert pulse._pulse_enabled() is False
