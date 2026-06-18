#!/usr/bin/env python3
"""OSN Weekly Metrics Digest -- DM Matt with store-by-store performance.

Fires weekly on Monday at 15:00 UTC (8am AZ). Reports the most-recently
completed week (Mon-Sun) versus the prior completed week, ranked by revenue,
for all 4 OSN stores.

Source change (2026-06-17): the prior point-of-sale connector was retired, so
revenue now comes from each store's books (accrual P&L). Booked revenue will not
line up 1:1 with the old register totals, and per-transaction count / average
ticket are no longer available -- the digest reports revenue + week-over-week
only. See the source note in the message body.

Usage (Windows Task Scheduler):
    python scripts/run_osn_metrics_digest.py [--dry-run]

Environment variables required:
    SLACK_BOT_TOKEN                For sending DMs
    (QBO tokens in .credentials/qbo-tokens.json, refreshed daily)
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

# Importing a cora module installs the egress sanitizer (cora/__init__.py), so the
# WebClient DM below is routed through the egress boundary like every other send.
from cora.tools.qbo_client import (  # noqa: E402
    QboClientError,
    extract_pnl_revenue,
    get_profit_loss,
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

# OSN store realms. Each store is a SEPARATE QBO company (its own realm); the
# entity code is the key into .credentials/qbo-tokens.json.
STORE_NAMES: dict[str, str] = {
    "OSNGW": "Gilbert & Warner",
    "OSNGM": "Gilbert & McKellips",
    "OSNGF": "Greenfield & 60",
    "OSNVV": "Val Vista & Pecos",
}
_OSN_ENTITIES: tuple[str, ...] = ("OSNGW", "OSNGM", "OSNGF", "OSNVV")


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


def _week_ranges(today: date) -> tuple[tuple[str, str], tuple[str, str], str]:
    """Return ((this_start, this_end), (prior_start, prior_end), week_of).

    "this week" = the most recently COMPLETED Mon-Sun (the week before the
    in-progress one), so a Monday-morning run compares two full weeks rather
    than a few hours of the current week against a full prior week. Dates are
    inclusive ISO strings; week_of is the Monday of the reported week.
    """
    current_monday = today - timedelta(days=today.weekday())
    last_sunday = current_monday - timedelta(days=1)
    last_monday = last_sunday - timedelta(days=6)
    prior_sunday = last_monday - timedelta(days=1)
    prior_monday = prior_sunday - timedelta(days=6)
    return (
        (last_monday.isoformat(), last_sunday.isoformat()),
        (prior_monday.isoformat(), prior_sunday.isoformat()),
        last_monday.isoformat(),
    )


def _fetch_week_revenue(start: str, end: str) -> dict[str, float]:
    """Return {entity_code: revenue_usd} for the OSN stores over [start, end].

    A store whose P&L call fails, or whose report has no Income line, is omitted
    (logged) rather than reported as $0 -- so a single token/API hiccup degrades
    one store, not the whole digest.
    """
    out: dict[str, float] = {}
    for entity in _OSN_ENTITIES:
        try:
            # Pin Accrual so all 4 stores are on the SAME basis (each is a separate
            # QBO company with its own default report preference) and the digest's
            # "accrual" label is actually true.
            report = get_profit_loss(entity, start, end, accounting_method="Accrual")
        except QboClientError as exc:
            log.warning("P&L fetch failed for %s (%s..%s): %s", entity, start, end, exc)
            continue
        revenue = extract_pnl_revenue(report)
        if revenue is None:
            log.warning("No income line in P&L for %s (%s..%s)", entity, start, end)
            continue
        out[entity] = revenue
    return out


def build_message(
    this_week: dict[str, float],
    last_week: dict[str, float],
    week_of: str,
    missing: list[str] | None = None,
) -> str:
    """Build the DM text for Matt.

    this_week / last_week map entity code (OSNGW/OSNGM/OSNGF/OSNVV) -> revenue.
    missing: store codes that returned no data this week (failed fetch / no Income
    section). Surfaced explicitly so a partial-outage week is not mistaken for a
    complete picture, and the total is flagged as covering only N of the stores.
    """
    missing = missing or []
    stores: list[dict] = []
    for code, revenue in this_week.items():
        lw_rev = last_week.get(code, 0.0)
        stores.append({
            "store_code": code,
            "store_name": _store_label(STORE_NAMES.get(code, code)),
            "revenue": revenue,
            "wow_pct": _calc_wow_pct(revenue, lw_rev),
        })

    stores.sort(key=lambda x: x["revenue"], reverse=True)

    lines = [f":convenience_store: *OSN Weekly Metrics -- Week of {week_of} (Mon-Sun)*", ""]
    lines.append("*Store Rankings (revenue):*")

    for rank, store in enumerate(stores, start=1):
        lines.append(
            f"{rank}. {store['store_name']:<22} "
            f"{_format_currency(store['revenue'])} ({_format_wow(store['wow_pct'])})"
        )

    lines.append("")

    if missing:
        names = ", ".join(_store_label(STORE_NAMES.get(c, c)) for c in missing)
        lines.append(f":grey_question: *No data this week for:* {names} (will retry next run)")

    total_this = sum(s["revenue"] for s in stores)
    total_last = sum(last_week.get(s["store_code"], 0.0) for s in stores)
    total_suffix = f" ({len(stores)} of {len(_OSN_ENTITIES)} stores)" if missing else ""
    lines.append(
        f"*Total for the week:* {_format_currency(total_this)} | "
        f"WoW: {_format_wow(_calc_wow_pct(total_this, total_last))}{total_suffix}"
    )

    flagged = [s for s in stores if s["wow_pct"] is not None and s["wow_pct"] < WOW_FLAG_THRESHOLD]
    if flagged:
        lines.append("")
        lines.append(":warning: *Flagged stores (>10% decline):*")
        for s in flagged:
            lines.append(f"  - {s['store_name']}: {_format_wow(s['wow_pct'])} WoW")

    lines.append("")
    lines.append(
        "_Revenue now reflects booked (accrual) sales and won't match the prior "
        "point-of-sale totals 1:1; transaction count and average ticket are no "
        "longer included._"
    )

    return "\n".join(lines)


def send_dm(slack_client, user_id: str, text: str, dry_run: bool = False) -> None:
    if dry_run:
        log.info("[DRY RUN] Would DM %s:\n%s", user_id, text)
        return
    resp = slack_client.conversations_open(users=[user_id])
    dm_channel = resp["channel"]["id"]
    slack_client.chat_postMessage(channel=dm_channel, text=text)
    log.info("Sent OSN digest DM to %s via channel %s", user_id, dm_channel)


def run(dry_run: bool = False, today: date | None = None) -> dict[str, int]:
    import os

    from slack_sdk import WebClient

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        log.error("SLACK_BOT_TOKEN not set")
        return {"stores_fetched": 0, "error": 1}

    today = today or date.today()
    (this_start, this_end), (prior_start, prior_end), week_of = _week_ranges(today)

    this_week = _fetch_week_revenue(this_start, this_end)
    if not this_week:
        log.warning(
            "No OSN store revenue for week %s..%s -- skipping DM", this_start, this_end
        )
        return {"stores_fetched": 0, "error": 0}

    last_week = _fetch_week_revenue(prior_start, prior_end)

    missing = [code for code in _OSN_ENTITIES if code not in this_week]
    message = build_message(this_week, last_week, week_of, missing=missing)

    slack_client = WebClient(token=bot_token)
    try:
        send_dm(slack_client, MATT_SLACK_ID, message, dry_run=dry_run)
    except Exception as exc:
        log.error("Failed to send DM to Matt: %s", exc)
        return {"stores_fetched": len(this_week), "error": 1}

    return {"stores_fetched": len(this_week), "error": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OSN Weekly Metrics Digest")
    parser.add_argument("--dry-run", action="store_true", help="Log without sending DM")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    log.info("osn_metrics_digest result: %s", result)
