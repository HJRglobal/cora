#!/usr/bin/env python3
"""Clover Daily Store Summary -- Post yesterday's OSN sales to #osn-leadership.

Fetches yesterday's sales for all 4 OSN stores, computes totals and
per-store averages, flags any store performing >20% below the mean,
and posts a formatted table to #osn-leadership.

Usage (Windows Task Scheduler):
    python scripts/run_clover_daily_summary.py [--dry-run]

Environment variables required:
    CLOVER_*          Per-store Clover credentials (see clover_client.py)
    SLACK_BOT_TOKEN   For posting to Slack
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
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
log = logging.getLogger("clover_daily_summary")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_OSN_CHANNEL = "C0B3TCEF4KT"   # #osn-leadership
_UNDERPERFORM_THRESHOLD = 0.20  # 20% below mean = flag

# Human-readable store labels
STORE_LABELS = {
    "GW":  "G & Warner",
    "GM":  "G & McKellips",
    "GF":  "Greenfield & 60",
    "VVP": "Val Vista & Pecos",
}

# Column widths for table formatting
_COL_STORE = 20
_COL_REV = 9
_COL_TXNS = 5
_COL_AVG = 10


def _az_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-7)))


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _format_row(
    label: str,
    revenue: float,
    txns: int,
    avg_ticket: float,
    flag: str = "",
) -> str:
    """Return a fixed-width Slack table row."""
    rev_str = f"${revenue:,.0f}"
    avg_str = f"${avg_ticket:.2f}"
    flag_str = f"  {flag}" if flag else ""
    return (
        f"{label:<{_COL_STORE}}"
        f"| {rev_str:>{_COL_REV}} "
        f"| {str(txns):>{_COL_TXNS}} "
        f"| {avg_str:>{_COL_AVG}}"
        f"{flag_str}"
    )


def build_summary_message(summaries: list, date_label: str) -> str:
    """Build the full Slack message for the daily summary.

    Args:
        summaries: list of StoreSalesSummary objects (from clover_client)
        date_label: human-readable date string e.g. "2026-06-02"

    Returns:
        Formatted Slack message string.
    """
    if not summaries:
        return f":convenience_store: *OSN Daily Sales Summary -- {date_label}*\n\nNo sales data available."

    # Compute total
    total_rev = sum(s.net_revenue_usd for s in summaries)
    total_txns = sum(s.transaction_count for s in summaries)
    total_avg = total_rev / total_txns if total_txns > 0 else 0.0

    # Compute mean revenue for underperformance detection
    mean_rev = total_rev / len(summaries) if summaries else 0.0

    # Header
    header = (
        f":convenience_store: *OSN Daily Sales Summary -- {date_label}*\n\n"
        f"```\n"
        f"{'Store':<{_COL_STORE}}| {'Revenue':>{_COL_REV}} | {'Txns':>{_COL_TXNS}} | {'Avg Ticket':>{_COL_AVG}}\n"
        f"{'-' * (_COL_STORE + _COL_REV + _COL_TXNS + _COL_AVG + 12)}\n"
    )

    rows = []
    underperformers = []
    for s in summaries:
        label = STORE_LABELS.get(s.store_code, s.store_name)
        flag = ""
        if mean_rev > 0 and s.net_revenue_usd < mean_rev * (1 - _UNDERPERFORM_THRESHOLD):
            flag = "⚠️"
            underperformers.append(label)
        rows.append(_format_row(label, s.net_revenue_usd, s.transaction_count, s.avg_ticket_usd, flag))

    separator = "-" * (_COL_STORE + _COL_REV + _COL_TXNS + _COL_AVG + 12)
    total_row = _format_row("TOTAL", total_rev, total_txns, total_avg)

    body = "\n".join(rows) + f"\n{separator}\n{total_row}\n```"

    msg = header + body

    if underperformers:
        msg += f"\n\n:warning: *{', '.join(underperformers)}* performing >20% below store average."

    return msg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> dict[str, Any]:
    from slack_sdk import WebClient as SlackWebClient
    from cora.connectors.clover_client import (
        get_all_stores_sales_pulse,
        CloverConnectorError,
    )

    bot_token = os.getenv("SLACK_BOT_TOKEN")
    if not bot_token:
        log.error("SLACK_BOT_TOKEN not set")
        return {"error": "SLACK_BOT_TOKEN not set"}

    slack = SlackWebClient(token=bot_token)

    # Get yesterday's date label
    now_az = _az_now()
    yesterday = now_az - timedelta(days=1)
    date_label = yesterday.strftime("%Y-%m-%d")

    try:
        summaries = get_all_stores_sales_pulse("yesterday")
    except CloverConnectorError as exc:
        log.error("Clover error fetching yesterday sales: %s", exc)
        return {"error": str(exc)}
    except Exception as exc:
        log.error("Unexpected error fetching Clover data: %s", exc)
        return {"error": str(exc)}

    msg = build_summary_message(summaries, date_label)
    log.info("Built summary for %s: %d stores", date_label, len(summaries))

    if dry_run:
        log.info("[DRY-RUN] Would post to %s:\n%s", _OSN_CHANNEL, msg[:500])
        return {"dry_run": True, "stores": len(summaries), "date": date_label}

    try:
        slack.chat_postMessage(channel=_OSN_CHANNEL, text=msg)
        log.info("Posted Clover daily summary to #osn-leadership")
        return {"posted": True, "stores": len(summaries), "date": date_label}
    except Exception as exc:
        log.error("Failed to post summary: %s", exc)
        return {"error": str(exc)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cora Clover daily store summary")
    parser.add_argument("--dry-run", action="store_true", help="Log without posting")
    args = parser.parse_args()
    result = run(dry_run=args.dry_run)
    log.info("Result: %s", result)
    sys.exit(0)
