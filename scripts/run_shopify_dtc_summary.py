#!/usr/bin/env python3
"""Shopify DTC Daily Summary + Milestone Celebrations.

Daily:
  1. Post yesterday's DTC sales summary to #f3e-leadership.
  2. Check order/revenue milestones and celebrate first-crossings
     via Slack + SQLite state.

Milestone state: data/shopify_milestones.db (SQLite)
  - Order milestones: 1st, 10th, 25th, 100th order (cumulative this month)
  - Revenue milestones: first week with $1K, $5K, $10K revenue

Usage (Windows Task Scheduler):
    python scripts/run_shopify_dtc_summary.py [--dry-run]

Environment variables required:
    SHOPIFY_F3E_ACCESS_TOKEN     Shopify Admin API offline access token
    SHOPIFY_F3E_STORE            Store domain (e.g. f3energy.myshopify.com)
    SLACK_BOT_TOKEN              For posting to Slack
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

LOG_DIR = _REPO_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s -- %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            LOG_DIR / f"cora-{datetime.now().strftime('%Y-%m-%d')}.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("shopify_dtc_summary")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_F3E_CHANNEL = "C0B4KRQT3LY"    # #f3e-leadership
_MILESTONE_DB_PATH = _REPO_ROOT / "data" / "shopify_milestones.db"

# Order milestones (cumulative this month)
ORDER_MILESTONES = [1, 10, 25, 100]

# Weekly revenue milestones (this week)
REVENUE_WEEK_MILESTONES = [1000, 5000, 10000]

MILESTONE_EMOJIS = {
    "orders_1":              ":tada:",
    "orders_10":             ":fire:",
    "orders_25":             ":rocket:",
    "orders_100":            ":100:",
    "revenue_week_1000":     ":moneybag:",
    "revenue_week_5000":     ":star:",
    "revenue_week_10000":    ":crown:",
}

MILESTONE_DESCRIPTIONS = {
    "orders_1":              "First ever DTC order! The F3 store is live!",
    "orders_10":             "10 orders! Double digits!",
    "orders_25":             "25 orders this month!",
    "orders_100":            "100 orders this month! Triple digits!",
    "revenue_week_1000":     "First week crossing $1,000 in DTC revenue!",
    "revenue_week_5000":     "First week crossing $5,000 in DTC revenue!",
    "revenue_week_10000":    "First week crossing $10,000 in DTC revenue!",
}


def _az_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-7)))


# ---------------------------------------------------------------------------
# SQLite milestone state
# ---------------------------------------------------------------------------

def _open_db() -> sqlite3.Connection:
    _MILESTONE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_MILESTONE_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS milestones (
            milestone_key TEXT PRIMARY KEY,
            celebrated_at INTEGER
        )
    """)
    conn.commit()
    return conn


def _has_celebrated(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute(
        "SELECT celebrated_at FROM milestones WHERE milestone_key = ?", (key,)
    ).fetchone()
    return row is not None


def _record_celebration(conn: sqlite3.Connection, key: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO milestones (milestone_key, celebrated_at) VALUES (?, ?)",
        (key, int(time.time())),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Daily summary message
# ---------------------------------------------------------------------------

def build_daily_summary(
    yesterday_summary,
    week_summary,
    date_label: str,
) -> str:
    """Build the daily DTC summary Slack message."""
    orders = yesterday_summary.order_count
    net_rev = yesterday_summary.net_revenue_usd
    aov = yesterday_summary.avg_order_value_usd

    # vs last week daily average
    week_orders = week_summary.order_count
    week_rev = week_summary.net_revenue_usd
    # Divide by 7 for daily average
    wk_avg_orders = week_orders / 7
    wk_avg_rev = week_rev / 7

    order_delta = 0.0
    rev_delta = 0.0
    if wk_avg_orders > 0:
        order_delta = (orders - wk_avg_orders) / wk_avg_orders * 100
    if wk_avg_rev > 0:
        rev_delta = (net_rev - wk_avg_rev) / wk_avg_rev * 100

    top_skus = ""
    if yesterday_summary.top_products:
        top = yesterday_summary.top_products[0]
        top_skus = f"\nTop SKU: {top.title} ({top.quantity_sold} units)"

    msg = (
        f":shopping_cart: *F3E DTC Daily Summary -- {date_label}*\n"
        f"Yesterday: {orders} order(s) | ${net_rev:,.0f} net | AOV ${aov:.2f}"
        f"{top_skus}\n"
        f"vs last week avg: {order_delta:+.0f}% orders, {rev_delta:+.0f}% revenue"
    )
    return msg


# ---------------------------------------------------------------------------
# Milestone check
# ---------------------------------------------------------------------------

def check_and_celebrate_milestones(
    conn: sqlite3.Connection,
    slack_client,
    month_summary,
    week_summary,
    dry_run: bool,
) -> list[str]:
    """Check milestones and post celebrations. Returns list of keys celebrated."""
    celebrated = []

    # Order milestones (cumulative this month)
    month_orders = month_summary.order_count
    for threshold in ORDER_MILESTONES:
        key = f"orders_{threshold}"
        if month_orders >= threshold and not _has_celebrated(conn, key):
            emoji = MILESTONE_EMOJIS.get(key, ":tada:")
            desc = MILESTONE_DESCRIPTIONS.get(key, key)
            msg = (
                f"{emoji} *F3E DTC Milestone!* {emoji}\n"
                f"{emoji} {desc}\n"
                f"Keep it up, team! :rocket:"
            )
            if _post_to_slack(slack_client, _F3E_CHANNEL, msg, dry_run):
                if not dry_run:
                    _record_celebration(conn, key)
                celebrated.append(key)
                log.info("Celebrated milestone: %s (orders=%d)", key, month_orders)

    # Revenue week milestones (this week)
    week_rev = week_summary.net_revenue_usd
    for threshold in REVENUE_WEEK_MILESTONES:
        key = f"revenue_week_{threshold}"
        if week_rev >= threshold and not _has_celebrated(conn, key):
            emoji = MILESTONE_EMOJIS.get(key, ":moneybag:")
            desc = MILESTONE_DESCRIPTIONS.get(key, key)
            msg = (
                f"{emoji} *F3E DTC Milestone!* {emoji}\n"
                f"{emoji} {desc}\n"
                f"Keep it up, team! :rocket:"
            )
            if _post_to_slack(slack_client, _F3E_CHANNEL, msg, dry_run):
                if not dry_run:
                    _record_celebration(conn, key)
                celebrated.append(key)
                log.info("Celebrated milestone: %s (week_rev=%.0f)", key, week_rev)

    return celebrated


def _post_to_slack(slack_client, channel: str, text: str, dry_run: bool) -> bool:
    if dry_run:
        log.info("[DRY-RUN] Post to %s: %.120s", channel, text)
        return True
    try:
        slack_client.chat_postMessage(channel=channel, text=text)
        return True
    except Exception as exc:
        log.warning("Failed to post to %s: %s", channel, exc)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> dict[str, Any]:
    from slack_sdk import WebClient as SlackWebClient
    from cora.connectors.shopify_client import (
        get_sales_pulse,
        ShopifyConnectorError,
        ShopifyConfigError,
    )

    bot_token = os.getenv("SLACK_BOT_TOKEN")
    if not bot_token:
        log.error("SLACK_BOT_TOKEN not set")
        return {"error": "SLACK_BOT_TOKEN not set"}

    slack = SlackWebClient(token=bot_token)
    now_az = _az_now()
    yesterday = now_az - timedelta(days=1)
    date_label = yesterday.strftime("%Y-%m-%d")

    results: dict[str, Any] = {
        "date": date_label,
        "summary_posted": False,
        "milestones_celebrated": [],
        "errors": [],
    }

    # Fetch data
    try:
        yesterday_data = get_sales_pulse("yesterday")
        week_data = get_sales_pulse("7d")
        month_data = get_sales_pulse("30d")
    except (ShopifyConnectorError, ShopifyConfigError) as exc:
        log.error("Shopify data fetch error: %s", exc)
        results["errors"].append(str(exc))
        return results
    except Exception as exc:
        log.error("Unexpected error fetching Shopify data: %s", exc)
        results["errors"].append(str(exc))
        return results

    # Post daily summary
    summary_msg = build_daily_summary(yesterday_data, week_data, date_label)
    if _post_to_slack(slack, _F3E_CHANNEL, summary_msg, dry_run):
        results["summary_posted"] = True
        log.info("Posted DTC daily summary for %s", date_label)

    # Check milestones
    conn = _open_db()
    try:
        celebrated = check_and_celebrate_milestones(
            conn, slack, month_data, week_data, dry_run
        )
        results["milestones_celebrated"] = celebrated
    finally:
        conn.close()

    log.info("Shopify DTC summary complete: %s", results)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cora Shopify DTC daily summary")
    parser.add_argument("--dry-run", action="store_true", help="Log without posting")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
    sys.exit(0)
