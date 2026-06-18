"""Tests for run_osn_metrics_digest.py -- OSN weekly metrics on QBO P&L.

Rebuilt 2026-06-17 (Phase 3 item C): the prior Clover (point-of-sale) source was
retired. The digest now derives per-store revenue + WoW from each store's QBO
P&L; transaction count + average ticket are gone (no QBO equivalent).
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import run_osn_metrics_digest as osn  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures -- minimal QBO P&L report envelopes
# ---------------------------------------------------------------------------

def _pnl(income: float) -> dict:
    """A minimal QBO P&L report with a single Income section."""
    return {"Rows": {"Row": [
        {
            "type": "Section",
            "Header": {"ColData": [{"value": "Income"}]},
            "Summary": {"ColData": [{"value": "Total Income"}, {"value": str(income)}]},
        },
    ]}}


def _pnl_no_income() -> dict:
    """A P&L report with no Income section (e.g. an expense-only sub-ledger)."""
    return {"Rows": {"Row": [
        {
            "type": "Section",
            "Header": {"ColData": [{"value": "Expenses"}]},
            "Summary": {"ColData": [{"value": "Total Expenses"}, {"value": "1000"}]},
        },
    ]}}


# ---------------------------------------------------------------------------
# _calc_wow_pct
# ---------------------------------------------------------------------------

def test_calc_wow_pct_positive():
    assert abs(osn._calc_wow_pct(1100.0, 1000.0) - 10.0) < 0.01


def test_calc_wow_pct_negative():
    assert abs(osn._calc_wow_pct(900.0, 1000.0) + 10.0) < 0.01


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
    assert "⚠️" in osn._format_wow(-15.0)


def test_format_wow_no_flag_above_threshold():
    assert "⚠️" not in osn._format_wow(-5.0)


# ---------------------------------------------------------------------------
# _store_label
# ---------------------------------------------------------------------------

def test_store_label_known():
    assert osn._store_label("Gilbert & Warner") == "G & Warner"


def test_store_label_unknown():
    assert osn._store_label("Unknown Store") == "Unknown Store"


# ---------------------------------------------------------------------------
# _week_ranges -- completed-week comparison
# ---------------------------------------------------------------------------

def test_week_ranges_fired_monday():
    (this_s, this_e), (prior_s, prior_e), week_of = osn._week_ranges(date(2026, 6, 15))  # Monday
    assert (this_s, this_e) == ("2026-06-08", "2026-06-14")
    assert (prior_s, prior_e) == ("2026-06-01", "2026-06-07")
    assert week_of == "2026-06-08"


def test_week_ranges_fired_midweek():
    # Wednesday 2026-06-17 -- still reports the last *completed* week (6/8-6/14)
    (this_s, this_e), (prior_s, prior_e), _ = osn._week_ranges(date(2026, 6, 17))
    assert (this_s, this_e) == ("2026-06-08", "2026-06-14")
    assert (prior_s, prior_e) == ("2026-06-01", "2026-06-07")


def test_week_ranges_are_seven_days_each():
    (this_s, this_e), (prior_s, prior_e), _ = osn._week_ranges(date(2026, 6, 15))
    assert (date.fromisoformat(this_e) - date.fromisoformat(this_s)).days == 6
    assert (date.fromisoformat(prior_e) - date.fromisoformat(prior_s)).days == 6
    # prior week ends the day before this week starts -- contiguous, no gap/overlap
    assert (date.fromisoformat(this_s) - date.fromisoformat(prior_e)).days == 1


# ---------------------------------------------------------------------------
# _fetch_week_revenue
# ---------------------------------------------------------------------------

def test_fetch_week_revenue_extracts_income():
    reports = {
        "OSNGW": _pnl(5000.0),
        "OSNGM": _pnl(4000.0),
        "OSNGF": _pnl(3000.0),
        "OSNVV": _pnl(2000.0),
    }
    with patch.object(osn, "get_profit_loss", side_effect=lambda e, s, en, **kw: reports[e]):
        out = osn._fetch_week_revenue("2026-06-08", "2026-06-14")
    assert out == {"OSNGW": 5000.0, "OSNGM": 4000.0, "OSNGF": 3000.0, "OSNVV": 2000.0}


def test_fetch_week_revenue_skips_store_with_no_income():
    reports = {
        "OSNGW": _pnl(5000.0),
        "OSNGM": _pnl_no_income(),
        "OSNGF": _pnl(3000.0),
        "OSNVV": _pnl(2000.0),
    }
    with patch.object(osn, "get_profit_loss", side_effect=lambda e, s, en, **kw: reports[e]):
        out = osn._fetch_week_revenue("2026-06-08", "2026-06-14")
    assert "OSNGM" not in out
    assert out == {"OSNGW": 5000.0, "OSNGF": 3000.0, "OSNVV": 2000.0}


def test_fetch_week_revenue_skips_store_on_api_error():
    def _se(entity, start, end, **kwargs):
        if entity == "OSNGM":
            raise osn.QboClientError("realm down")
        return _pnl(1000.0)

    with patch.object(osn, "get_profit_loss", side_effect=_se):
        out = osn._fetch_week_revenue("2026-06-08", "2026-06-14")
    assert "OSNGM" not in out
    assert len(out) == 3


def test_fetch_week_revenue_keeps_zero_revenue_store():
    # A genuine zero-revenue week (0.0) is KEPT (contrast: no-income -> dropped).
    reports = {
        "OSNGW": _pnl(5000.0),
        "OSNGM": _pnl(0.0),
        "OSNGF": _pnl(3000.0),
        "OSNVV": _pnl(2000.0),
    }
    with patch.object(osn, "get_profit_loss", side_effect=lambda e, s, en, **kw: reports[e]):
        out = osn._fetch_week_revenue("2026-06-08", "2026-06-14")
    assert "OSNGM" in out
    assert out["OSNGM"] == 0.0


def test_fetch_week_revenue_pins_accrual():
    mock = MagicMock(return_value=_pnl(1000.0))
    with patch.object(osn, "get_profit_loss", mock):
        osn._fetch_week_revenue("2026-06-08", "2026-06-14")
    # Every store call pins Accrual so the 4 separate realms are on one basis.
    assert mock.call_count == 4
    for call in mock.call_args_list:
        assert call.kwargs.get("accounting_method") == "Accrual"


# ---------------------------------------------------------------------------
# build_message
# ---------------------------------------------------------------------------

def test_build_message_contains_header_and_week():
    msg = osn.build_message({"OSNGW": 4231.0}, {"OSNGW": 4000.0}, "2026-06-08")
    assert "OSN Weekly Metrics" in msg
    assert "2026-06-08" in msg


def test_build_message_sorted_by_revenue_descending():
    this = {"OSNGW": 1000.0, "OSNGF": 4000.0, "OSNGM": 2000.0, "OSNVV": 3000.0}
    msg = osn.build_message(this, this, "2026-06-08")
    rank1 = next(line for line in msg.split("\n") if line.startswith("1."))
    assert "Greenfield & 60" in rank1  # the $4,000 store (OSNGF)


def test_build_message_contains_total():
    msg = osn.build_message({"OSNGW": 4231.0}, {"OSNGW": 4000.0}, "2026-06-08")
    assert "Total for the week" in msg


def test_build_message_flags_big_decline():
    this = {"OSNGW": 4000.0, "OSNGM": 3000.0, "OSNGF": 2000.0, "OSNVV": 500.0}
    last = {"OSNGW": 4000.0, "OSNGM": 3000.0, "OSNGF": 2000.0, "OSNVV": 2000.0}
    msg = osn.build_message(this, last, "2026-06-08")
    assert "Flagged" in msg
    assert "⚠️" in msg


def test_build_message_no_flags_when_stable():
    this = {"OSNGW": 4000.0, "OSNGM": 3900.0, "OSNGF": 3500.0, "OSNVV": 3000.0}
    msg = osn.build_message(this, this, "2026-06-08")
    assert "Flagged" not in msg


def test_build_message_no_last_week_data():
    msg = osn.build_message({"OSNGW": 4000.0}, {}, "2026-06-08")
    assert "OSN Weekly Metrics" in msg
    assert "--" in msg  # WoW shows "--" when there is no prior-week figure


def test_build_message_drops_txns_and_aov():
    msg = osn.build_message({"OSNGW": 4000.0}, {"OSNGW": 4000.0}, "2026-06-08").lower()
    assert "txns" not in msg
    assert "aov" not in msg
    assert "avg ticket" not in msg


def test_build_message_labels_source_change():
    msg = osn.build_message({"OSNGW": 4000.0}, {"OSNGW": 4000.0}, "2026-06-08")
    assert "accrual" in msg.lower()


def test_build_message_is_source_opaque():
    msg = osn.build_message({"OSNGW": 4000.0}, {"OSNGW": 3000.0}, "2026-06-08").lower()
    for banned in ("clover", "quickbooks", "qbo", "realm", "intuit", "merchant"):
        assert banned not in msg


def test_build_message_surfaces_missing_stores_and_partial_total():
    msg = osn.build_message(
        {"OSNGW": 4000.0, "OSNGF": 3000.0}, {"OSNGW": 4000.0, "OSNGF": 3000.0},
        "2026-06-08", missing=["OSNGM", "OSNVV"],
    )
    assert "No data this week for" in msg
    assert "G & McKellips" in msg     # OSNGM display name
    assert "Val Vista & Pecos" in msg  # OSNVV display name
    assert "2 of 4 stores" in msg      # total flagged as partial


def test_build_message_no_missing_line_when_complete():
    msg = osn.build_message(
        {"OSNGW": 4000.0}, {"OSNGW": 4000.0}, "2026-06-08", missing=[]
    )
    assert "No data this week for" not in msg
    assert "of 4 stores" not in msg
    assert "Total for the week:" in msg


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
    with patch.object(osn, "get_profit_loss", return_value=_pnl(2000.0)), \
         patch("slack_sdk.WebClient") as mock_wc:
        client = _mock_slack()
        mock_wc.return_value = client
        osn.run(dry_run=True, today=date(2026, 6, 15))
    client.conversations_open.assert_not_called()


def test_run_sends_dm_to_matt(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    with patch.object(osn, "get_profit_loss", return_value=_pnl(2000.0)), \
         patch("slack_sdk.WebClient") as mock_wc:
        client = _mock_slack()
        mock_wc.return_value = client
        result = osn.run(dry_run=False, today=date(2026, 6, 15))
    client.conversations_open.assert_called_once_with(users=["U0B3PS7RFJA"])
    client.chat_postMessage.assert_called_once()
    assert result["stores_fetched"] == 4
    assert result["error"] == 0


def test_run_no_stores_skips_dm(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    with patch.object(osn, "get_profit_loss", side_effect=osn.QboClientError("down")), \
         patch("slack_sdk.WebClient") as mock_wc:
        client = _mock_slack()
        mock_wc.return_value = client
        result = osn.run(dry_run=False, today=date(2026, 6, 15))
    client.conversations_open.assert_not_called()
    assert result["stores_fetched"] == 0
    assert result["error"] == 0


def test_run_partial_outage_surfaces_missing(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    def _se(entity, start, end, **kw):
        if entity == "OSNVV":
            raise osn.QboClientError("realm down")
        return _pnl(2000.0)

    client = _mock_slack()
    with patch.object(osn, "get_profit_loss", side_effect=_se), \
         patch("slack_sdk.WebClient", return_value=client):
        result = osn.run(dry_run=False, today=date(2026, 6, 15))

    assert result["stores_fetched"] == 3  # 3 of 4 succeeded
    sent = client.chat_postMessage.call_args.kwargs["text"]
    assert "No data this week for" in sent
    assert "Val Vista & Pecos" in sent   # the missing store is named
    assert "3 of 4 stores" in sent        # total flagged partial


def test_run_dm_failure_returns_error(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    client = _mock_slack()
    client.chat_postMessage.side_effect = Exception("slack down")
    with patch.object(osn, "get_profit_loss", return_value=_pnl(2000.0)), \
         patch("slack_sdk.WebClient", return_value=client):
        result = osn.run(dry_run=False, today=date(2026, 6, 15))
    assert result["error"] == 1
