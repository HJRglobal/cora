"""Tests for Feature #10: Clover Daily Store Summary -> #osn-leadership."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest


# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

def _import_module():
    import importlib.util, sys
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "run_clover_daily_summary",
        Path(__file__).resolve().parents[1] / "scripts" / "run_clover_daily_summary.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_clover_daily_summary"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _import_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_summary(code, name, revenue, txns, avg_ticket=None):
    from cora.connectors.clover_client import StoreSalesSummary
    if avg_ticket is None:
        avg_ticket = revenue / txns if txns > 0 else 0.0
    return StoreSalesSummary(
        store_code=code,
        store_name=name,
        period="yesterday",
        revenue_usd=revenue,
        transaction_count=txns,
        avg_ticket_usd=avg_ticket,
        refund_total_usd=0.0,
        refund_count=0,
        net_revenue_usd=revenue,
    )


def _four_stores(revenues=(1200, 1100, 1050, 950)):
    return [
        _make_summary("GW", "Gilbert & Warner", revenues[0], 45),
        _make_summary("GM", "G & McKellips", revenues[1], 40),
        _make_summary("GF", "Greenfield & 60", revenues[2], 38),
        _make_summary("VVP", "Val Vista & Pecos", revenues[3], 35),
    ]


def _make_slack():
    slack = MagicMock()
    slack.chat_postMessage.return_value = {"ok": True}
    return slack


# ---------------------------------------------------------------------------
# build_summary_message tests
# ---------------------------------------------------------------------------

class TestBuildSummaryMessage:
    def test_contains_date_label(self):
        summaries = _four_stores()
        msg = mod.build_summary_message(summaries, "2026-06-02")
        assert "2026-06-02" in msg

    def test_contains_all_store_labels(self):
        summaries = _four_stores()
        msg = mod.build_summary_message(summaries, "2026-06-02")
        assert "G & Warner" in msg
        assert "G & McKellips" in msg
        assert "Greenfield & 60" in msg
        assert "Val Vista & Pecos" in msg

    def test_contains_total_row(self):
        summaries = _four_stores()
        msg = mod.build_summary_message(summaries, "2026-06-02")
        assert "TOTAL" in msg

    def test_total_revenue_correct(self):
        summaries = _four_stores((1000, 1000, 1000, 1000))
        msg = mod.build_summary_message(summaries, "2026-06-02")
        assert "4,000" in msg

    def test_underperforming_store_flagged(self):
        # One store with 50% of the mean (well below 20% threshold)
        summaries = [
            _make_summary("GW", "G & Warner", 2000, 70),
            _make_summary("GM", "G & McKellips", 2000, 70),
            _make_summary("GF", "Greenfield & 60", 2000, 70),
            _make_summary("VVP", "Val Vista & Pecos", 500, 20),  # WAY below mean
        ]
        msg = mod.build_summary_message(summaries, "2026-06-02")
        assert "⚠️" in msg
        assert "Val Vista & Pecos" in msg

    def test_all_equal_no_flag(self):
        summaries = _four_stores((1000, 1000, 1000, 1000))
        msg = mod.build_summary_message(summaries, "2026-06-02")
        # No store below 20% threshold when all equal
        assert "⚠️" not in msg or msg.count("⚠️") == 0

    def test_empty_summaries(self):
        msg = mod.build_summary_message([], "2026-06-02")
        assert "No sales data" in msg

    def test_contains_revenue_values(self):
        summaries = [_make_summary("GW", "G & Warner", 1234, 45)]
        msg = mod.build_summary_message(summaries, "2026-06-02")
        assert "1,234" in msg

    def test_contains_transaction_count(self):
        summaries = [_make_summary("GW", "G & Warner", 1000, 42)]
        msg = mod.build_summary_message(summaries, "2026-06-02")
        assert "42" in msg

    def test_contains_avg_ticket(self):
        summaries = [_make_summary("GW", "G & Warner", 1000, 40, avg_ticket=25.0)]
        msg = mod.build_summary_message(summaries, "2026-06-02")
        assert "25.00" in msg

    def test_header_contains_emoji(self):
        summaries = _four_stores()
        msg = mod.build_summary_message(summaries, "2026-06-02")
        assert ":convenience_store:" in msg

    def test_single_store_no_comparison_flag(self):
        """With only 1 store, underperformance detection is skipped (no mean to compare to)."""
        summaries = [_make_summary("GW", "G & Warner", 100, 5)]
        msg = mod.build_summary_message(summaries, "2026-06-02")
        # Should not flag the only store
        assert "⚠️" not in msg

    def test_underperforming_flag_in_warning_line(self):
        summaries = [
            _make_summary("GW", "G & Warner", 2000, 70),
            _make_summary("GM", "G & McKellips", 2000, 70),
            _make_summary("GF", "Greenfield & 60", 2000, 70),
            _make_summary("VVP", "Val Vista & Pecos", 100, 5),
        ]
        msg = mod.build_summary_message(summaries, "2026-06-02")
        assert "performing >20% below" in msg


# ---------------------------------------------------------------------------
# run() tests
# ---------------------------------------------------------------------------

class TestRun:
    def test_posts_to_osn_channel(self):
        slack = _make_slack()
        summaries = _four_stores()

        with patch("cora.connectors.clover_client.get_all_stores_sales_pulse", return_value=summaries), \
             patch("slack_sdk.WebClient", return_value=slack):
            result = mod.run(dry_run=False)

        assert result.get("posted") is True
        slack.chat_postMessage.assert_called_once()
        assert slack.chat_postMessage.call_args.kwargs["channel"] == mod._OSN_CHANNEL

    def test_dry_run_no_post(self):
        slack = _make_slack()
        summaries = _four_stores()

        with patch("cora.connectors.clover_client.get_all_stores_sales_pulse", return_value=summaries), \
             patch("slack_sdk.WebClient", return_value=slack):
            result = mod.run(dry_run=True)

        slack.chat_postMessage.assert_not_called()
        assert result.get("dry_run") is True

    def test_clover_error_returns_error(self):
        from cora.connectors.clover_client import CloverConnectorError
        slack = _make_slack()

        with patch("cora.connectors.clover_client.get_all_stores_sales_pulse",
                   side_effect=CloverConnectorError("API error")), \
             patch("slack_sdk.WebClient", return_value=slack):
            result = mod.run(dry_run=False)

        assert "error" in result
        slack.chat_postMessage.assert_not_called()

    def test_missing_slack_token_returns_error(self):
        import os
        token = os.environ.pop("SLACK_BOT_TOKEN", None)
        try:
            result = mod.run(dry_run=False)
            assert "error" in result
        finally:
            if token:
                os.environ["SLACK_BOT_TOKEN"] = token

    def test_result_contains_store_count(self):
        slack = _make_slack()
        summaries = _four_stores()

        with patch("cora.connectors.clover_client.get_all_stores_sales_pulse", return_value=summaries), \
             patch("slack_sdk.WebClient", return_value=slack):
            result = mod.run(dry_run=False)

        assert result.get("stores") == 4

    def test_message_content_posted(self):
        slack = _make_slack()
        summaries = _four_stores()

        with patch("cora.connectors.clover_client.get_all_stores_sales_pulse", return_value=summaries), \
             patch("slack_sdk.WebClient", return_value=slack):
            mod.run(dry_run=False)

        posted_text = slack.chat_postMessage.call_args.kwargs["text"]
        assert "OSN Daily Sales Summary" in posted_text

    def test_unexpected_error_returns_error(self):
        slack = _make_slack()

        with patch("cora.connectors.clover_client.get_all_stores_sales_pulse",
                   side_effect=RuntimeError("network timeout")), \
             patch("slack_sdk.WebClient", return_value=slack):
            result = mod.run(dry_run=False)

        assert "error" in result
