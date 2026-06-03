"""Tests for Feature #11: Shopify DTC Daily Summary + Milestone Celebrations."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

def _import_module():
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location(
        "run_shopify_dtc_summary",
        Path(__file__).resolve().parents[1] / "scripts" / "run_shopify_dtc_summary.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_shopify_dtc_summary"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _import_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sales_summary(
    period="yesterday",
    order_count=5,
    gross_revenue=200.0,
    net_revenue=190.0,
    aov=38.0,
    top_product_title="F3 Energy",
    top_product_qty=10,
):
    from cora.connectors.shopify_client import SalesSummary, TopProduct
    return SalesSummary(
        period=period,
        order_count=order_count,
        gross_revenue_usd=gross_revenue,
        discounts_usd=5.0,
        refunds_usd=5.0,
        net_revenue_usd=net_revenue,
        avg_order_value_usd=aov,
        top_products=[TopProduct(title=top_product_title, quantity_sold=top_product_qty, sku="SKU1")]
        if top_product_title else [],
    )


def _make_slack():
    slack = MagicMock()
    slack.chat_postMessage.return_value = {"ok": True}
    return slack


def _fresh_db(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "milestones.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS milestones (
            milestone_key TEXT PRIMARY KEY,
            celebrated_at INTEGER
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# SQLite helpers tests
# ---------------------------------------------------------------------------

class TestMilestoneDb:
    def test_has_celebrated_false_for_new_key(self, tmp_path):
        conn = _fresh_db(tmp_path)
        assert mod._has_celebrated(conn, "orders_1") is False
        conn.close()

    def test_has_celebrated_true_after_record(self, tmp_path):
        conn = _fresh_db(tmp_path)
        mod._record_celebration(conn, "orders_1")
        assert mod._has_celebrated(conn, "orders_1") is True
        conn.close()

    def test_multiple_milestones_independent(self, tmp_path):
        conn = _fresh_db(tmp_path)
        mod._record_celebration(conn, "orders_1")
        assert mod._has_celebrated(conn, "orders_1") is True
        assert mod._has_celebrated(conn, "orders_10") is False
        conn.close()

    def test_celebrate_at_timestamp_set(self, tmp_path):
        conn = _fresh_db(tmp_path)
        before = int(time.time())
        mod._record_celebration(conn, "orders_25")
        row = conn.execute("SELECT celebrated_at FROM milestones WHERE milestone_key='orders_25'").fetchone()
        assert row[0] >= before
        conn.close()

    def test_open_db_creates_table(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "_MILESTONE_DB_PATH", tmp_path / "test_milestones.db")
        conn = mod._open_db()
        # Verify table exists
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='milestones'").fetchone()
        assert row is not None
        conn.close()


# ---------------------------------------------------------------------------
# build_daily_summary tests
# ---------------------------------------------------------------------------

class TestBuildDailySummary:
    def test_contains_date(self):
        yd = _make_sales_summary(order_count=3, net_revenue=150)
        wk = _make_sales_summary(period="7d", order_count=21, net_revenue=1050)
        msg = mod.build_daily_summary(yd, wk, "2026-06-02")
        assert "2026-06-02" in msg

    def test_contains_order_count(self):
        yd = _make_sales_summary(order_count=7, net_revenue=350)
        wk = _make_sales_summary(period="7d", order_count=35, net_revenue=1750)
        msg = mod.build_daily_summary(yd, wk, "2026-06-02")
        assert "7 order" in msg

    def test_contains_revenue(self):
        yd = _make_sales_summary(order_count=5, net_revenue=250)
        wk = _make_sales_summary(period="7d", order_count=35, net_revenue=1750)
        msg = mod.build_daily_summary(yd, wk, "2026-06-02")
        assert "250" in msg

    def test_contains_top_sku(self):
        yd = _make_sales_summary(top_product_title="F3 Pure Variety", top_product_qty=3)
        wk = _make_sales_summary(period="7d")
        msg = mod.build_daily_summary(yd, wk, "2026-06-02")
        assert "F3 Pure Variety" in msg

    def test_contains_vs_comparison(self):
        yd = _make_sales_summary(order_count=10, net_revenue=500)
        wk = _make_sales_summary(period="7d", order_count=70, net_revenue=3500)
        msg = mod.build_daily_summary(yd, wk, "2026-06-02")
        assert "vs last week avg" in msg

    def test_header_emoji(self):
        yd = _make_sales_summary()
        wk = _make_sales_summary(period="7d")
        msg = mod.build_daily_summary(yd, wk, "2026-06-02")
        assert ":shopping_cart:" in msg

    def test_positive_delta_shown(self):
        yd = _make_sales_summary(order_count=20, net_revenue=1000)
        wk = _make_sales_summary(period="7d", order_count=70, net_revenue=3500)
        msg = mod.build_daily_summary(yd, wk, "2026-06-02")
        assert "+" in msg

    def test_no_top_product(self):
        from cora.connectors.shopify_client import SalesSummary
        yd = SalesSummary(
            period="yesterday", order_count=2, gross_revenue_usd=100,
            discounts_usd=0, refunds_usd=0, net_revenue_usd=100,
            avg_order_value_usd=50, top_products=[]
        )
        wk = _make_sales_summary(period="7d")
        msg = mod.build_daily_summary(yd, wk, "2026-06-02")
        assert "F3E DTC Daily Summary" in msg


# ---------------------------------------------------------------------------
# check_and_celebrate_milestones tests
# ---------------------------------------------------------------------------

class TestCheckMilestones:
    def test_first_order_celebrated(self, tmp_path):
        conn = _fresh_db(tmp_path)
        slack = _make_slack()
        month = _make_sales_summary(period="30d", order_count=1)
        week = _make_sales_summary(period="7d", net_revenue=50)
        celebrated = mod.check_and_celebrate_milestones(conn, slack, month, week, dry_run=False)
        assert "orders_1" in celebrated
        conn.close()

    def test_not_celebrated_twice(self, tmp_path):
        conn = _fresh_db(tmp_path)
        slack = _make_slack()
        mod._record_celebration(conn, "orders_1")
        month = _make_sales_summary(period="30d", order_count=5)
        week = _make_sales_summary(period="7d", net_revenue=50)
        celebrated = mod.check_and_celebrate_milestones(conn, slack, month, week, dry_run=False)
        assert "orders_1" not in celebrated
        conn.close()

    def test_revenue_milestone_celebrated(self, tmp_path):
        conn = _fresh_db(tmp_path)
        slack = _make_slack()
        month = _make_sales_summary(period="30d", order_count=0)
        week = _make_sales_summary(period="7d", net_revenue=1500)
        celebrated = mod.check_and_celebrate_milestones(conn, slack, month, week, dry_run=False)
        assert "revenue_week_1000" in celebrated
        conn.close()

    def test_below_threshold_not_celebrated(self, tmp_path):
        conn = _fresh_db(tmp_path)
        slack = _make_slack()
        month = _make_sales_summary(period="30d", order_count=0)
        week = _make_sales_summary(period="7d", net_revenue=500)
        celebrated = mod.check_and_celebrate_milestones(conn, slack, month, week, dry_run=False)
        assert "revenue_week_1000" not in celebrated
        conn.close()

    def test_dry_run_does_not_record(self, tmp_path):
        conn = _fresh_db(tmp_path)
        slack = _make_slack()
        month = _make_sales_summary(period="30d", order_count=1)
        week = _make_sales_summary(period="7d", net_revenue=50)
        mod.check_and_celebrate_milestones(conn, slack, month, week, dry_run=True)
        assert mod._has_celebrated(conn, "orders_1") is False
        conn.close()

    def test_celebration_message_posted_to_f3e_channel(self, tmp_path):
        conn = _fresh_db(tmp_path)
        slack = _make_slack()
        month = _make_sales_summary(period="30d", order_count=1)
        week = _make_sales_summary(period="7d", net_revenue=50)
        mod.check_and_celebrate_milestones(conn, slack, month, week, dry_run=False)
        call_kwargs = slack.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == mod._F3E_CHANNEL
        conn.close()

    def test_100_order_milestone(self, tmp_path):
        conn = _fresh_db(tmp_path)
        slack = _make_slack()
        month = _make_sales_summary(period="30d", order_count=100)
        week = _make_sales_summary(period="7d", net_revenue=50)
        celebrated = mod.check_and_celebrate_milestones(conn, slack, month, week, dry_run=False)
        assert "orders_100" in celebrated
        conn.close()

    def test_multiple_milestones_at_once(self, tmp_path):
        conn = _fresh_db(tmp_path)
        slack = _make_slack()
        # 10 orders AND $1k week revenue
        month = _make_sales_summary(period="30d", order_count=10)
        week = _make_sales_summary(period="7d", net_revenue=1200)
        celebrated = mod.check_and_celebrate_milestones(conn, slack, month, week, dry_run=False)
        assert "orders_1" in celebrated
        assert "orders_10" in celebrated
        assert "revenue_week_1000" in celebrated
        conn.close()


# ---------------------------------------------------------------------------
# run() integration tests
# ---------------------------------------------------------------------------

class TestRun:
    def test_happy_path_posts_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "_MILESTONE_DB_PATH", tmp_path / "milestones.db")
        slack = _make_slack()
        yd = _make_sales_summary()
        wk = _make_sales_summary(period="7d")
        mo = _make_sales_summary(period="30d", order_count=5)

        with patch("cora.connectors.shopify_client.get_sales_pulse") as mock_pulse, \
             patch("slack_sdk.WebClient", return_value=slack):
            mock_pulse.side_effect = [yd, wk, mo]
            result = mod.run(dry_run=False)

        assert result["summary_posted"] is True

    def test_dry_run_does_not_post(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "_MILESTONE_DB_PATH", tmp_path / "milestones.db")
        slack = _make_slack()
        yd = _make_sales_summary()
        wk = _make_sales_summary(period="7d")
        mo = _make_sales_summary(period="30d", order_count=5)

        with patch("cora.connectors.shopify_client.get_sales_pulse") as mock_pulse, \
             patch("slack_sdk.WebClient", return_value=slack):
            mock_pulse.side_effect = [yd, wk, mo]
            result = mod.run(dry_run=True)

        slack.chat_postMessage.assert_not_called()

    def test_shopify_error_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "_MILESTONE_DB_PATH", tmp_path / "milestones.db")
        slack = _make_slack()
        from cora.connectors.shopify_client import ShopifyConnectorError

        with patch("cora.connectors.shopify_client.get_sales_pulse",
                   side_effect=ShopifyConnectorError("token invalid")), \
             patch("slack_sdk.WebClient", return_value=slack):
            result = mod.run(dry_run=False)

        assert len(result["errors"]) > 0
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

    def test_first_order_triggers_celebration(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "_MILESTONE_DB_PATH", tmp_path / "milestones.db")
        slack = _make_slack()
        yd = _make_sales_summary()
        wk = _make_sales_summary(period="7d")
        mo = _make_sales_summary(period="30d", order_count=1)

        with patch("cora.connectors.shopify_client.get_sales_pulse") as mock_pulse, \
             patch("slack_sdk.WebClient", return_value=slack):
            mock_pulse.side_effect = [yd, wk, mo]
            result = mod.run(dry_run=False)

        assert "orders_1" in result["milestones_celebrated"]
        # Summary + milestone post
        assert slack.chat_postMessage.call_count >= 2

    def test_result_contains_expected_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "_MILESTONE_DB_PATH", tmp_path / "milestones.db")
        slack = _make_slack()
        yd = _make_sales_summary()
        wk = _make_sales_summary(period="7d")
        mo = _make_sales_summary(period="30d")

        with patch("cora.connectors.shopify_client.get_sales_pulse") as mock_pulse, \
             patch("slack_sdk.WebClient", return_value=slack):
            mock_pulse.side_effect = [yd, wk, mo]
            result = mod.run(dry_run=False)

        assert "date" in result
        assert "summary_posted" in result
        assert "milestones_celebrated" in result
        assert "errors" in result
