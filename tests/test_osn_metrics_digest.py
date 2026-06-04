"""Tests for run_osn_metrics_digest.py -- Feature #17."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import run_osn_metrics_digest as osn  # noqa: E402
from cora.connectors.clover_client import StoreSalesSummary  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_store(code: str, name: str, revenue: float, txns: int, refunds: float = 0.0) -> StoreSalesSummary:
    from dataclasses import dataclass
    return StoreSalesSummary(
        store_code=code,
        store_name=name,
        period="this_week",
        revenue_usd=revenue,
        transaction_count=txns,
        avg_ticket_usd=revenue / txns if txns > 0 else 0.0,
        refund_total_usd=refunds,
        refund_count=0,
        net_revenue_usd=revenue - refunds,
    )


def _make_four_stores(revenues: list[float] | None = None) -> list[StoreSalesSummary]:
    if revenues is None:
        revenues = [4231.0, 3890.0, 3550.0, 2910.0]
    stores_data = [
        ("GW",  "Gilbert & Warner"),
        ("GF",  "Greenfield & 60"),
        ("MK",  "Gilbert & McKellips"),
        ("VVP", "Val Vista & Pecos"),
    ]
    return [
        _make_store(code, name, rev, 150)
        for (code, name), rev in zip(stores_data, revenues)
    ]


# ---------------------------------------------------------------------------
# _calc_wow_pct
# ---------------------------------------------------------------------------

def test_calc_wow_pct_positive():
    result = osn._calc_wow_pct(1100.0, 1000.0)
    assert abs(result - 10.0) < 0.01


def test_calc_wow_pct_negative():
    result = osn._calc_wow_pct(900.0, 1000.0)
    assert abs(result + 10.0) < 0.01


def test_calc_wow_pct_zero_last_week():
    assert osn._calc_wow_pct(500.0, 0.0) is None


def test_calc_wow_pct_same():
    assert osn._calc_wow_pct(1000.0, 1000.0) == 0.0


# ---------------------------------------------------------------------------
# _format_wow
# ---------------------------------------------------------------------------

def test_format_wow_positive():
    assert "+" in osn._format_wow(10.0)


def test_format_wow_negative():
    assert "-" in osn._format_wow(-5.0)


def test_format_wow_none():
    assert osn._format_wow(None) == "--"


def test_format_wow_flag_below_threshold():
    result = osn._format_wow(-15.0)
    assert "⚠️" in result


def test_format_wow_no_flag_above_threshold():
    result = osn._format_wow(-5.0)
    assert "⚠️" not in result


# ---------------------------------------------------------------------------
# _store_label
# ---------------------------------------------------------------------------

def test_store_label_known():
    assert osn._store_label("Gilbert & Warner") == "G & Warner"


def test_store_label_unknown():
    assert osn._store_label("Unknown Store") == "Unknown Store"


# ---------------------------------------------------------------------------
# build_message
# ---------------------------------------------------------------------------

def test_build_message_contains_header():
    this_week = _make_four_stores()
    last_week = _make_four_stores()
    msg = osn.build_message(this_week, last_week, "2026-06-02")
    assert "OSN Weekly Metrics" in msg


def test_build_message_contains_week_of():
    this_week = _make_four_stores()
    last_week = _make_four_stores()
    msg = osn.build_message(this_week, last_week, "2026-06-02")
    assert "2026-06-02" in msg


def test_build_message_sorted_by_revenue_descending():
    revenues = [1000.0, 4000.0, 2000.0, 3000.0]
    this_week = _make_four_stores(revenues)
    last_week = _make_four_stores(revenues)
    msg = osn.build_message(this_week, last_week, "2026-06-02")
    # "1." should be in front of the highest-revenue store
    lines = msg.split("\n")
    rank1_line = next((l for l in lines if l.startswith("1.")), "")
    assert "4,000" in rank1_line or "Greenfield" in rank1_line  # $4000 store is GF


def test_build_message_contains_total():
    this_week = _make_four_stores()
    last_week = _make_four_stores()
    msg = osn.build_message(this_week, last_week, "2026-06-02")
    assert "Total this week" in msg


def test_build_message_flags_big_decline():
    this_week = _make_four_stores([4000.0, 3000.0, 2000.0, 500.0])   # VVP dropped a lot
    last_week = _make_four_stores([4000.0, 3000.0, 2000.0, 2000.0])
    msg = osn.build_message(this_week, last_week, "2026-06-02")
    assert "Flagged" in msg
    assert "⚠️" in msg


def test_build_message_no_flags_when_stable():
    this_week = _make_four_stores([4000.0, 3900.0, 3500.0, 3000.0])
    last_week = _make_four_stores([4000.0, 3900.0, 3500.0, 3000.0])
    msg = osn.build_message(this_week, last_week, "2026-06-02")
    assert "Flagged" not in msg


def test_build_message_no_last_week_data():
    this_week = _make_four_stores()
    msg = osn.build_message(this_week, [], "2026-06-02")
    # Should still produce output without crashing
    assert "OSN Weekly Metrics" in msg


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------

def _mock_slack():
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "D_MATT"}}
    return client


def test_run_no_token(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    result = osn.run(dry_run=True)
    assert result.get("error") == 1


def test_run_dry_run_no_dm_sent(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    stores = _make_four_stores()

    with patch.object(osn, "get_all_stores_sales_pulse", return_value=stores), \
         patch("slack_sdk.WebClient") as mock_wc:
        client = _mock_slack()
        mock_wc.return_value = client
        osn.run(dry_run=True)

    client.conversations_open.assert_not_called()


def test_run_sends_dm_to_matt(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    stores = _make_four_stores()

    with patch.object(osn, "get_all_stores_sales_pulse", return_value=stores), \
         patch("slack_sdk.WebClient") as mock_wc:
        client = _mock_slack()
        mock_wc.return_value = client
        result = osn.run(dry_run=False)

    client.conversations_open.assert_called_once_with(users=["U0B3PS7RFJA"])
    assert result["stores_fetched"] == 4


def test_run_no_stores_skips_dm(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    with patch.object(osn, "get_all_stores_sales_pulse", return_value=[]), \
         patch("slack_sdk.WebClient") as mock_wc:
        client = _mock_slack()
        mock_wc.return_value = client
        result = osn.run(dry_run=False)

    client.conversations_open.assert_not_called()
    assert result["stores_fetched"] == 0


def test_run_clover_error(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    with patch.object(osn, "get_all_stores_sales_pulse", side_effect=osn.CloverConnectorError("fail")), \
         patch("slack_sdk.WebClient") as mock_wc:
        mock_wc.return_value = _mock_slack()
        result = osn.run(dry_run=False)

    assert result.get("error") == 1
