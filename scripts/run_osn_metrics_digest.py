#!/usr/bin/env python3
"""OSN Weekly Metrics Digest -- DM Matt with store-by-store performance.

Fires weekly on Monday at 15:00 UTC (8am AZ).
Compares this week vs last week for all 4 OSN stores.

Usage (Windows Task Scheduler):
    python scripts/run_osn_metrics_digest.py [--dry-run]

Environment variables required:
    CLOVER_*_MERCHANT_ID / CLOVER_*_API_KEY    Per-store Clover credentials
    SLACK_BOT_TOKEN                             For sending DMs
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

from cora.connectors.clover_client import (  # noqa: E402
    CloverConnectorError,
    StoreSalesSummary,
    get_all_stores_sales_pulse,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("osn_metrics_digest")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MATT_SLACK_ID = "U0B3PS7RFJA"
WOW_FLAG_THRESHOLD = -10.0  # percent WoW decline that triggers a warning flag


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _calc_wow_pct(this_week: float, last_week: float) -> float | None:
    """Return WoW percent change, or None if last_week is 0."""
    if last_week == 0:
        return None
    return ((this_week - last_week) / last_week) * 100


def _format_wow(pct: float | None) -> str:
    if pct is None:
        return "--"
    sign = "+" if pct >= 0 else ""
    flag = "⚠️" if pct < WOW_FLAG_THRESHOLD else ""
    return f"{sign}{pct:.0f}%{flag}"


def _format_currency(value: float) -> str:
    return f"${value:,.0f}"


def _store_label(store_name: str) -> str:
    """Shorten store_name for display."""
    replacements = {
        "Gilbert & Warner": "G & Warner",
        "Gilbert & McKellips": "G & McKellips",
        "Greenfield & 60": "Greenfield & 60",
        "Val Vista & Pecos": "Val Vista & Pecos",
    }
    return replacements.get(store_name, store_name)


def build_message(
    this_week: list[StoreSalesSummary],
    last_week: list[StoreSalesSummary],
    week_of: str,
) -> str:
    """Build the DM text for Matt."""
    # Build lookup maps by store_code
    lw_map = {s.store_code: s for s in last_week}

    # Calculate WoW per store and sort by this week's revenue descending
    stores: list[dict[str, Any]] = []
    for s in this_week:
        lw = lw_map.get(s.store_code)
        lw_rev = lw.net_revenue_usd if lw else 0.0
        wow_pct = _calc_wow_pct(s.net_revenue_usd, lw_rev)
        aov = s.net_revenue_usd / s.transaction_count if s.transaction_count > 0 else 0.0
        stores.append({
            "store_code": s.store_code,
            "store_name": _store_label(s.store_name),
            "revenue": s.net_revenue_usd,
            "txns": s.transaction_count,
            "aov": aov,
            "wow_pct": wow_pct,
        })

    stores.sort(key=lambda x: x["revenue"], reverse=True)

    lines = [f":convenience_store: *OSN Weekly Metrics -- Week of {week_of}*", ""]
    lines.append("*Store Rankings (this week):*")

    for rank, store in enumerate(stores, start=1):
        wow_str = _format_wow(store["wow_pct"])
        lines.append(
            f"{rank}. {store['store_name']:<22} "
            f"{_format_currency(store['revenue'])} ({wow_str}) | "
            f"{store['txns']} txns | "
            f"${store['aov']:.2f} AOV"
        )

    lines.append("")

    # Totals
    total_this = sum(s["revenue"] for s in stores)
    total_last = sum((lw_map.get(s["store_code"]).net_revenue_usd if lw_map.get(s["store_code"]) else 0.0)
                     for s in stores)
    wow_total = _calc_wow_pct(total_this, total_last)

    lines.append(
        f"*Total this week:* {_format_currency(total_this)} | "
        f"WoW: {_format_wow(wow_total)}"
    )

    # Flag warnings
    flagged = [s for s in stores if s["wow_pct"] is not None and s["wow_pct"] < WOW_FLAG_THRESHOLD]
    if flagged:
        lines.append("")
        lines.append(":warning: *Flagged stores (>10% decline):*")
        for s in flagged:
            lines.append(f"  - {s['store_name']}: {_format_wow(s['wow_pct'])} WoW")

    return "\n".join(lines)


def send_dm(slack_client, user_id: str, text: str, dry_run: bool = False) -> None:
    if dry_run:
        log.info("[DRY RUN] Would DM %s:\n%s", user_id, text)
        return
    resp = slack_client.conversations_open(users=[user_id])
    dm_channel = resp["channel"]["id"]
    slack_client.chat_postMessage(channel=dm_channel, text=text)
    log.info("Sent OSN digest DM to %s via channel %s", user_id, dm_channel)


def run(dry_run: bool = False) -> dict[str, int]:
    from slack_sdk import WebClient

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        log.error("SLACK_BOT_TOKEN not set")
        return {"stores_fetched": 0, "error": 1}

    slack_client = WebClient(token=bot_token)

    try:
        this_week_stores = get_all_stores_sales_pulse("this_week")
    except CloverConnectorError as exc:
        log.error("Failed to fetch this_week Clover data: %s", exc)
        return {"stores_fetched": 0, "error": 1}

    try:
        last_week_stores = get_all_stores_sales_pulse("last_week")
    except CloverConnectorError as exc:
        log.warning("Failed to fetch last_week data: %s", exc)
        last_week_stores = []

    if not this_week_stores:
        log.warning("No stores returned for this_week -- skipping DM")
        return {"stores_fetched": 0, "error": 0}

    week_of = (date.today() - timedelta(days=date.today().weekday())).isoformat()

    message = build_message(this_week_stores, last_week_stores, week_of)

    try:
        send_dm(slack_client, MATT_SLACK_ID, message, dry_run=dry_run)
    except Exception as exc:
        log.error("Failed to send DM to Matt: %s", exc)
        return {"stores_fetched": len(this_week_stores), "error": 1}

    return {"stores_fetched": len(this_week_stores), "error": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OSN Weekly Metrics Digest")
    parser.add_argument("--dry-run", action="store_true", help="Log without sending DM")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    log.info("osn_metrics_digest result: %s", result)
