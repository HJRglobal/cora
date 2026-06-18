#!/usr/bin/env python3
"""[F3 Energy] Daily Ecom + Ops brief -> Slack.

The Slack-native twin of the Cowork ecom-ops cockpit. Composes a compact daily
brief from existing Cora connectors (DTC / Paid / Subs / Retail / Inventory /
Production / Ops) and posts it once daily to #f3-ops-cockpit.

Guardrails:
  - Source-opaque. #f3-ops-cockpit is NOT a finance-tier channel, so the cash line
    is OMITTED here (it points to #f3-finance). No platform/sheet names.
  - Entity [F3 Energy]. The post is routed through the egress boundary: importing a
    cora module installs the sanitizer (cora/__init__), so the WebClient post below
    is auto-sanitized like every other send (D-032).
  - Fail-soft: every section degrades to a "not available" line on its source's
    error, so one dead connector never blocks the brief. Polar (Paid + Subs)
    renders "Polar not connected yet" when credentials are absent / unauthed.
  - D-029: this is intelligence (multi-source synthesis), not a mechanical
    single-source push. Retire the standalone Make DTC scenario once this is live.

Usage:
    .venv\\Scripts\\python.exe scripts/run_f3e_ecom_brief.py [--dry-run] [--channel C...]
    --dry-run        read sources + print the brief; post nothing
    --channel CID    override the target channel (smoke to #cora-build = C0B4B0URRQS)

Environment (all optional; each missing source degrades gracefully):
    SLACK_BOT_TOKEN            post
    SHOPIFY_F3E_ACCESS_TOKEN   DTC + inventory
    POLAR_CLIENT_ID/SECRET     paid + subscriptions  (or POLAR_API_KEY)
    HUBSPOT_PRIVATE_APP_TOKEN  retail pipeline
    ASANA_PAT                  production / ops
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

# Importing cora modules installs the egress sanitizer (cora/__init__), so the
# WebClient post below is routed through the egress boundary.
from cora.connectors import polar_client, shopify_client  # noqa: E402
from cora.tools import asana_client, hubspot_client  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("f3e_ecom_brief")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COCKPIT_CHANNEL = "C0BC9KED61W"   # #f3-ops-cockpit (private; Cora is a member)
SMOKE_CHANNEL = "C0B4B0URRQS"     # #cora-build (smoke target)
RUN2_PROJECT_GID = "1215472268404903"  # [F3E] F3 Production - Run 2

_AZ_TZ = timezone(timedelta(hours=-7))  # Arizona = UTC-7 year-round (no DST)
_WINDOW_DAYS = 30

# Polar metric keys (validated 2026-06-17).
_PAID_METRICS = [
    "total_marketing_spend",
    "blended_net_sales",
    "blended_roas",
    "blended_total_orders",
]
_PAID_DIMENSION = "custom_internal-default-channel-grouping"
_SUB_METRICS = [
    "recharge_sales_products.computed.net_sales",
    "recharge_sales_products.raw.total_active_subscriptions",
    "recharge_sales_products.raw.new_subscriptions",
    "recharge_sales_products.computed.churned_subscriptions",
]

# F3E Retail pipeline closed stages (open pipeline = everything else).
_CLOSED_STAGE_IDS = frozenset({"3760235206", "3760235207"})  # Closed Won, Closed Lost

_INVENTORY_PREVIEW = 5  # cap low-stock SKUs named inline


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

def _az_today() -> date:
    return datetime.now(_AZ_TZ).date()


def _num(value: Any) -> float:
    """Coerce a Polar/HubSpot scalar to float; 0.0 on None/blank/unparseable."""
    if value is None:
        return 0.0
    try:
        return float(str(value).replace(",", "").replace("$", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _money(value: Any) -> str:
    return f"${_num(value):,.0f}"


def _pct_delta(current: Any, prior: Any) -> str:
    """'(+12%)' / '(-5%)' vs the prior window; '' when prior is 0/absent (no base)."""
    cur, pr = _num(current), _num(prior)
    if pr <= 0:
        return ""
    pct = (cur - pr) / pr * 100.0
    return f"({pct:+.0f}% vs prior {_WINDOW_DAYS}d)"


def _windows(today: date) -> tuple[tuple[str, str], tuple[str, str]]:
    """(current, prior) ISO date ranges for a trailing-30d vs prior-30d compare."""
    cur = ((today - timedelta(days=_WINDOW_DAYS)).isoformat(), today.isoformat())
    prior = (
        (today - timedelta(days=2 * _WINDOW_DAYS)).isoformat(),
        (today - timedelta(days=_WINDOW_DAYS)).isoformat(),
    )
    return cur, prior


# ---------------------------------------------------------------------------
# Section builders -- each fail-soft (returns a string; never raises)
# ---------------------------------------------------------------------------

def _dtc_line() -> str:
    try:
        d7 = shopify_client.get_sales_pulse("7d")
        d30 = shopify_client.get_sales_pulse("30d")
    except shopify_client.ShopifyConnectorError as exc:
        log.warning("DTC section unavailable: %s", exc)
        return "- *DTC (Shopify):* not available"
    top = d7.top_products[0].title if d7.top_products else "-"
    return (
        f"- *DTC (Shopify):* {_money(d7.net_revenue_usd)} net / {d7.order_count} ord / "
        f"{_money(d7.avg_order_value_usd)} AOV (7d) | 30d {_money(d30.net_revenue_usd)} net | top {top}"
    )


def _polar_report(metrics: list[str], dimensions: list[str], window: tuple[str, str]):
    return polar_client.generate_report(
        metrics=metrics,
        dimensions=dimensions,
        date_from=window[0],
        date_to=window[1],
        granularity="none",
    )


def _paid_line(today: date) -> str:
    cur_w, prior_w = _windows(today)
    try:
        cur = _polar_report(_PAID_METRICS, [_PAID_DIMENSION], cur_w)
        prior = _polar_report(_PAID_METRICS, [_PAID_DIMENSION], prior_w)
    except polar_client.PolarConnectorError as exc:
        log.warning("Paid section unavailable: %s", exc)
        return "- *Paid (Polar):* Polar not connected yet"

    spend = cur.total_data.get("total_marketing_spend")
    mer = _num(cur.total_data.get("blended_roas"))
    bnet = cur.total_data.get("blended_net_sales")
    bnet_prior = prior.total_data.get("blended_net_sales")

    # Top channel by spend (sort client-side; never trust ordering param shape).
    channels = [
        r for r in cur.table_data
        if _num(r.get("total_marketing_spend")) > 0 and r.get(_PAID_DIMENSION)
    ]
    channels.sort(key=lambda r: _num(r.get("total_marketing_spend")), reverse=True)
    top_channel = channels[0].get(_PAID_DIMENSION) if channels else "-"

    delta = _pct_delta(bnet, bnet_prior)
    return (
        f"- *Paid (Polar):* {_money(spend)} spend / {mer:.2f}x MER / "
        f"{_money(bnet)} blended net {delta} / top {top_channel} _(30d, blended)_"
    ).replace("  ", " ")


def _subs_line(today: date) -> str:
    cur_w, prior_w = _windows(today)
    try:
        cur = _polar_report(_SUB_METRICS, [], cur_w)
        prior = _polar_report(_SUB_METRICS, [], prior_w)
    except polar_client.PolarConnectorError as exc:
        log.warning("Subs section unavailable: %s", exc)
        return "- *Subs (ReCharge):* Polar not connected yet"

    net = cur.total_data.get("recharge_sales_products.computed.net_sales")
    net_prior = prior.total_data.get("recharge_sales_products.computed.net_sales")
    active = int(_num(cur.total_data.get("recharge_sales_products.raw.total_active_subscriptions")))
    delta = _pct_delta(net, net_prior)
    return f"- *Subs (ReCharge):* {_money(net)} net / {active} active {delta} _(30d)_".replace("  ", " ")


def _retail_line() -> str:
    try:
        deals = hubspot_client.get_deals_by_pipeline(hubspot_client.PIPELINE_F3E_RETAIL)
    except hubspot_client.HubSpotClientError as exc:
        log.warning("Retail section unavailable: %s", exc)
        return "- *Retail (HubSpot):* not available"

    open_deals = [
        d for d in deals
        if (d.get("properties") or {}).get("dealstage") not in _CLOSED_STAGE_IDS
    ]
    total = sum(_num((d.get("properties") or {}).get("amount")) for d in open_deals)
    open_deals.sort(key=lambda d: _num((d.get("properties") or {}).get("amount")), reverse=True)
    top3 = ", ".join(
        (d.get("properties") or {}).get("dealname", "?") for d in open_deals[:3]
    ) or "-"
    return f"- *Retail (HubSpot):* {_money(total)} open across {len(open_deals)} deals | hot: {top3}"


def _inventory_line() -> str:
    try:
        variants = shopify_client.get_inventory_status(low_stock_threshold=10)
    except shopify_client.ShopifyConnectorError as exc:
        log.warning("Inventory section unavailable: %s", exc)
        return "- *Inventory:* not available"

    low = [v for v in variants if getattr(v, "low_stock", False)]
    if not low:
        return "- *Inventory:* all healthy"
    low.sort(key=lambda v: getattr(v, "qty_on_hand", 0))
    named = "; ".join(
        f"{' '.join(p for p in (v.product_title, v.variant_title) if p)} ({v.qty_on_hand})"
        for v in low[:_INVENTORY_PREVIEW]
    )
    extra = len(low) - _INVENTORY_PREVIEW
    suffix = f" +{extra} more" if extra > 0 else ""
    return f"- *Inventory:* {len(low)} low/critical -- {named}{suffix}"


def _ops_lines(today: date) -> list[str]:
    try:
        tasks = asana_client.get_project_tasks(RUN2_PROJECT_GID, max_tasks=100)
    except asana_client.AsanaClientError as exc:
        log.warning("Ops section unavailable: %s", exc)
        return ["- *Production (Run-2):* not available"]

    open_tasks = [t for t in tasks if not t.get("completed")]
    overdue = []
    upcoming_due: list[date] = []
    for t in open_tasks:
        d = asana_client._parse_due_date(t.get("due_on") or "")
        if d is None:
            continue
        if d < today:
            overdue.append(t)
        else:
            upcoming_due.append(d)
    next_due = min(upcoming_due).isoformat() if upcoming_due else "-"

    prod = (
        f"- *Production (Run-2):* {len(open_tasks)} open, {len(overdue)} overdue "
        f"-- next due {next_due}"
    )
    if overdue:
        overdue_sorted = asana_client.sort_tasks_due_first(overdue)
        names = "; ".join(t.get("name", "?") for t in overdue_sorted[:3])
        ops = f"- *Ops:* overdue -- {names}"
    else:
        ops = "- *Ops:* none overdue"
    return [prod, ops]


def build_brief(today: date | None = None) -> str:
    today = today or _az_today()
    lines = [f"*[F3 Energy] Daily Ecom + Ops -- {today:%a %b} {today.day}*", ""]
    lines.append(_dtc_line())
    lines.append(_paid_line(today))
    lines.append(_subs_line(today))
    lines.append(_retail_line())
    lines.append(_inventory_line())
    lines.extend(_ops_lines(today))
    lines.append("")
    lines.append("_Cash -> #f3-finance_")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False, channel: str = COCKPIT_CHANNEL, today: date | None = None) -> dict[str, Any]:
    brief = build_brief(today)

    if dry_run:
        log.info("[DRY RUN] Would post to %s:\n%s", channel, brief)
        return {"posted": False, "channel": channel, "chars": len(brief)}

    from slack_sdk import WebClient

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        log.error("SLACK_BOT_TOKEN not set")
        return {"posted": False, "channel": channel, "error": "no_token"}

    slack_client = WebClient(token=bot_token)
    try:
        slack_client.chat_postMessage(channel=channel, text=brief)
        log.info("F3E ecom brief posted to %s (%d chars)", channel, len(brief))
        return {"posted": True, "channel": channel, "chars": len(brief)}
    except Exception as exc:
        log.error("Failed to post F3E ecom brief: %s", exc)
        return {"posted": False, "channel": channel, "error": str(exc)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="F3E Daily Ecom + Ops Brief")
    parser.add_argument("--dry-run", action="store_true", help="Read sources + print; post nothing")
    parser.add_argument(
        "--channel",
        default=COCKPIT_CHANNEL,
        help=f"Target channel id (default {COCKPIT_CHANNEL}; smoke to {SMOKE_CHANNEL})",
    )
    args = parser.parse_args()

    result = run(dry_run=args.dry_run, channel=args.channel)
    log.info("f3e_ecom_brief result: %s", result)
