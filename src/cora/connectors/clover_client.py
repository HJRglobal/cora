"""Clover POS connector for OSN — One Stop Nutrition (4 stores).

Reads sales, inventory, and customer data from the Clover REST API v3
on-demand (called at query time, not on a schedule). A 5-minute in-memory
cache per store+endpoint prevents hammering the API on back-to-back questions.

Stores:
  GW  — Gilbert & Warner
  GM  — Gilbert & McKellips
  GF  — Greenfield & 60
  VVP — Val Vista & Pecos

Auth: Bearer token per merchant (CLOVER_OSN_{STORE}_API_KEY env var).
Merchant IDs: CLOVER_OSN_{STORE}_MERCHANT_ID env var.

Behavioral contract (mirrors gsheets_financials.py):
  - Source-opaque: callers never surface merchant IDs, "Clover", or API details
  - All monetary values returned as float USD (Clover stores cents as int)
  - Raises CloverConnectorError on auth/API failure so tool handler can
    return the standard financial-gap response
  - AZ timezone (America/Phoenix, UTC-7 year-round — no DST) for all
    "today" / "yesterday" period calculations

Configuration (all in .env):
  CLOVER_OSN_GW_MERCHANT_ID, CLOVER_OSN_GW_API_KEY
  CLOVER_OSN_GM_MERCHANT_ID, CLOVER_OSN_GM_API_KEY
  CLOVER_OSN_GF_MERCHANT_ID, CLOVER_OSN_GF_API_KEY
  CLOVER_OSN_VVP_MERCHANT_ID, CLOVER_OSN_VVP_API_KEY
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLOVER_API_BASE = "https://api.clover.com/v3/merchants"
_CACHE_TTL_SECONDS = 300  # 5 minutes
_PAGE_LIMIT = 1000        # Clover max per page
_AZ_UTC_OFFSET = -7       # America/Phoenix — no DST

# Low-stock threshold: items at or below this qty get flagged
DEFAULT_LOW_STOCK_THRESHOLD = 5

VALID_STORES = ("GW", "GM", "GF", "VVP")
VALID_PERIODS = ("today", "yesterday", "7d", "30d")

STORE_NAMES: dict[str, str] = {
    "GW":  "Gilbert & Warner",
    "GM":  "Gilbert & McKellips",
    "GF":  "Greenfield & 60",
    "VVP": "Val Vista & Pecos",
}

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CloverConnectorError(Exception):
    """Raised when the Clover API is unreachable or returns an error."""


class CloverConfigError(CloverConnectorError):
    """Raised when required env vars are missing."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class StoreSalesSummary:
    store_code: str
    store_name: str
    period: str
    revenue_usd: float
    transaction_count: int
    avg_ticket_usd: float
    refund_total_usd: float
    refund_count: int
    net_revenue_usd: float


@dataclass
class InventoryItem:
    name: str
    sku: str
    qty_on_hand: int
    low_stock: bool
    price_usd: float


@dataclass
class StoreInventorySummary:
    store_code: str
    store_name: str
    total_items: int
    low_stock_items: list[InventoryItem] = field(default_factory=list)
    all_items: list[InventoryItem] = field(default_factory=list)


@dataclass
class CustomerPeriodStats:
    new_customers: int
    total_transactions: int
    prior_period_new_customers: Optional[int]
    pct_change: Optional[float]  # vs prior period, None if unavailable


@dataclass
class StoreCustomerSummary:
    store_code: str
    store_name: str
    period: str
    stats: CustomerPeriodStats


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, object]] = {}


def _cache_get(key: str) -> Optional[object]:
    entry = _cache.get(key)
    if entry and (time.monotonic() - entry[0]) < _CACHE_TTL_SECONDS:
        return entry[1]
    return None


def _cache_set(key: str, value: object) -> None:
    _cache[key] = (time.monotonic(), value)


def _cache_clear() -> None:
    """Clear all cached entries. Used in tests."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Store config helpers
# ---------------------------------------------------------------------------


def _store_config(store_code: str) -> tuple[str, str]:
    """Return (merchant_id, api_key) for a store code. Raises CloverConfigError."""
    code = store_code.upper()
    if code not in VALID_STORES:
        raise CloverConfigError(
            f"Unknown store code {store_code!r}. Valid: {', '.join(VALID_STORES)}"
        )
    mid = os.getenv(f"CLOVER_OSN_{code}_MERCHANT_ID", "")
    key = os.getenv(f"CLOVER_OSN_{code}_API_KEY", "")
    if not mid or not key:
        raise CloverConfigError(
            f"Missing env vars for store {code}: "
            f"CLOVER_OSN_{code}_MERCHANT_ID and/or CLOVER_OSN_{code}_API_KEY not set."
        )
    return mid, key


# ---------------------------------------------------------------------------
# Period helpers (AZ time — UTC-7, no DST)
# ---------------------------------------------------------------------------


def _az_now() -> datetime:
    az_tz = timezone(timedelta(hours=_AZ_UTC_OFFSET))
    return datetime.now(tz=az_tz)


def _period_to_epoch_ms(period: str) -> tuple[int, int]:
    """Return (start_epoch_ms, end_epoch_ms) for a named period in AZ time."""
    now = _az_now()
    az_tz = now.tzinfo

    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    elif period == "yesterday":
        yesterday = now - timedelta(days=1)
        start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        end = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
    elif period == "7d":
        start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    elif period == "30d":
        start = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    else:
        raise CloverConfigError(f"Unknown period {period!r}. Valid: {', '.join(VALID_PERIODS)}")

    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _prior_period_epoch_ms(period: str) -> tuple[int, int]:
    """Return epoch ms for the period immediately before the given one."""
    now = _az_now()

    if period == "today":
        # Prior period = yesterday
        return _period_to_epoch_ms("yesterday")
    elif period == "yesterday":
        day = now - timedelta(days=2)
        start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        end = day.replace(hour=23, minute=59, second=59, microsecond=999999)
    elif period == "7d":
        end_dt = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        start_dt = end_dt - timedelta(days=7)
        start, end = start_dt, end_dt
    elif period == "30d":
        end_dt = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        start_dt = end_dt - timedelta(days=30)
        start, end = start_dt, end_dt
    else:
        raise CloverConfigError(f"Unknown period {period!r}")

    if period in ("7d", "30d"):
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _get_all_pages(
    merchant_id: str,
    api_key: str,
    endpoint: str,
    extra_params: Optional[dict] = None,
) -> list[dict]:
    """Paginate through a Clover endpoint and return all items."""
    url = f"{_CLOVER_API_BASE}/{merchant_id}/{endpoint}"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    params = {"limit": _PAGE_LIMIT, "offset": 0}
    if extra_params:
        params.update(extra_params)

    results: list[dict] = []
    while True:
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
        except requests.RequestException as exc:
            raise CloverConnectorError(f"Network error calling {endpoint}: {exc}") from exc

        if resp.status_code == 401:
            raise CloverConnectorError(
                f"Auth failed for store (HTTP 401). Check API key for merchant {merchant_id[:6]}..."
            )
        if resp.status_code == 404:
            raise CloverConnectorError(
                f"Merchant not found (HTTP 404). Check merchant ID {merchant_id[:6]}..."
            )
        if not resp.ok:
            raise CloverConnectorError(
                f"Clover API error {resp.status_code} on {endpoint}: {resp.text[:200]}"
            )

        data = resp.json()
        elements = data.get("elements", [])
        results.extend(elements)

        # Stop if we got fewer than a full page — no more data
        if len(elements) < _PAGE_LIMIT:
            break
        params["offset"] += _PAGE_LIMIT

    return results


# ---------------------------------------------------------------------------
# Public API: Sales Pulse
# ---------------------------------------------------------------------------


def get_sales_pulse(
    store_code: str,
    period: str = "today",
) -> StoreSalesSummary:
    """Return sales summary for one store over a period.

    Fetches payments (revenue) and refunds separately, then computes
    transaction count, avg ticket, and net revenue.
    """
    cache_key = f"sales:{store_code.upper()}:{period}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    mid, key = _store_config(store_code)
    start_ms, end_ms = _period_to_epoch_ms(period)

    # ── Payments (successful charges) ───────────────────────────────────────
    payments = _get_all_pages(
        mid, key, "payments",
        extra_params={
            "filter": [
                f"createdTime>={start_ms}",
                f"createdTime<={end_ms}",
                "result=SUCCESS",
            ]
        },
    )

    revenue_cents = sum(p.get("amount", 0) for p in payments)
    txn_count = len(payments)

    # ── Refunds ──────────────────────────────────────────────────────────────
    refunds = _get_all_pages(
        mid, key, "refunds",
        extra_params={
            "filter": [
                f"createdTime>={start_ms}",
                f"createdTime<={end_ms}",
            ]
        },
    )

    refund_cents = sum(r.get("amount", 0) for r in refunds)
    refund_count = len(refunds)

    revenue_usd = revenue_cents / 100
    refund_usd = refund_cents / 100
    avg_ticket = revenue_usd / txn_count if txn_count > 0 else 0.0

    result = StoreSalesSummary(
        store_code=store_code.upper(),
        store_name=STORE_NAMES[store_code.upper()],
        period=period,
        revenue_usd=round(revenue_usd, 2),
        transaction_count=txn_count,
        avg_ticket_usd=round(avg_ticket, 2),
        refund_total_usd=round(refund_usd, 2),
        refund_count=refund_count,
        net_revenue_usd=round(revenue_usd - refund_usd, 2),
    )

    _cache_set(cache_key, result)
    log.info(
        "clover sales_pulse store=%s period=%s revenue=%.2f txns=%d",
        store_code, period, revenue_usd, txn_count,
    )
    return result


def get_all_stores_sales_pulse(period: str = "today") -> list[StoreSalesSummary]:
    """Return sales summaries for all 4 stores. Errors on individual stores
    are caught and logged; that store is skipped rather than failing everything."""
    summaries = []
    for code in VALID_STORES:
        try:
            summaries.append(get_sales_pulse(code, period))
        except CloverConnectorError as exc:
            log.warning("clover sales_pulse store=%s error: %s", code, exc)
    return summaries


# ---------------------------------------------------------------------------
# Public API: Inventory
# ---------------------------------------------------------------------------


def get_inventory(
    store_code: str,
    low_stock_threshold: int = DEFAULT_LOW_STOCK_THRESHOLD,
) -> StoreInventorySummary:
    """Return inventory levels for one store with low-stock flags."""
    cache_key = f"inventory:{store_code.upper()}:{low_stock_threshold}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    mid, key = _store_config(store_code)

    items_raw = _get_all_pages(
        mid, key, "inventory/items",
        extra_params={"expand": "itemStock"},
    )

    items: list[InventoryItem] = []
    for raw in items_raw:
        name = raw.get("name", "Unknown item")
        sku = raw.get("sku") or raw.get("alternateName") or ""
        price_cents = raw.get("price", 0)
        stock_info = raw.get("itemStock") or {}
        qty = stock_info.get("quantity", 0)

        items.append(InventoryItem(
            name=name,
            sku=sku,
            qty_on_hand=qty,
            low_stock=(qty <= low_stock_threshold),
            price_usd=round(price_cents / 100, 2),
        ))

    low_stock = [i for i in items if i.low_stock]

    result = StoreInventorySummary(
        store_code=store_code.upper(),
        store_name=STORE_NAMES[store_code.upper()],
        total_items=len(items),
        low_stock_items=low_stock,
        all_items=items,
    )

    _cache_set(cache_key, result)
    log.info(
        "clover inventory store=%s total_items=%d low_stock=%d",
        store_code, len(items), len(low_stock),
    )
    return result


def get_all_stores_inventory(
    low_stock_threshold: int = DEFAULT_LOW_STOCK_THRESHOLD,
) -> list[StoreInventorySummary]:
    """Return inventory summaries for all 4 stores."""
    summaries = []
    for code in VALID_STORES:
        try:
            summaries.append(get_inventory(code, low_stock_threshold))
        except CloverConnectorError as exc:
            log.warning("clover inventory store=%s error: %s", code, exc)
    return summaries


# ---------------------------------------------------------------------------
# Public API: Customer Trends
# ---------------------------------------------------------------------------


def get_customer_trends(
    store_code: str,
    period: str = "30d",
) -> StoreCustomerSummary:
    """Return new customer count + transaction count for a period,
    with comparison to the prior equivalent period."""
    cache_key = f"customers:{store_code.upper()}:{period}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    mid, key = _store_config(store_code)
    start_ms, end_ms = _period_to_epoch_ms(period)
    prior_start_ms, prior_end_ms = _prior_period_epoch_ms(period)

    # New customers = customers whose account was created in this period
    new_customers_raw = _get_all_pages(
        mid, key, "customers",
        extra_params={
            "filter": [
                f"createdTime>={start_ms}",
                f"createdTime<={end_ms}",
            ]
        },
    )
    new_count = len(new_customers_raw)

    # Transactions in period (for context alongside customer count)
    transactions_raw = _get_all_pages(
        mid, key, "payments",
        extra_params={
            "filter": [
                f"createdTime>={start_ms}",
                f"createdTime<={end_ms}",
                "result=SUCCESS",
            ]
        },
    )
    txn_count = len(transactions_raw)

    # Prior period new customers for MoM delta
    try:
        prior_customers_raw = _get_all_pages(
            mid, key, "customers",
            extra_params={
                "filter": [
                    f"createdTime>={prior_start_ms}",
                    f"createdTime<={prior_end_ms}",
                ]
            },
        )
        prior_count: Optional[int] = len(prior_customers_raw)
        if prior_count and prior_count > 0:
            pct_change: Optional[float] = round(
                ((new_count - prior_count) / prior_count) * 100, 1
            )
        else:
            pct_change = None
    except CloverConnectorError:
        prior_count = None
        pct_change = None

    stats = CustomerPeriodStats(
        new_customers=new_count,
        total_transactions=txn_count,
        prior_period_new_customers=prior_count,
        pct_change=pct_change,
    )

    result = StoreCustomerSummary(
        store_code=store_code.upper(),
        store_name=STORE_NAMES[store_code.upper()],
        period=period,
        stats=stats,
    )

    _cache_set(cache_key, result)
    log.info(
        "clover customer_trends store=%s period=%s new=%d prior=%s pct_change=%s",
        store_code, period, new_count, prior_count, pct_change,
    )
    return result


def get_all_stores_customer_trends(period: str = "30d") -> list[StoreCustomerSummary]:
    """Return customer trend summaries for all 4 stores."""
    summaries = []
    for code in VALID_STORES:
        try:
            summaries.append(get_customer_trends(code, period))
        except CloverConnectorError as exc:
            log.warning("clover customer_trends store=%s error: %s", code, exc)
    return summaries


# ---------------------------------------------------------------------------
# Formatting helpers (used by tool handlers)
# ---------------------------------------------------------------------------


def format_sales_for_llm(summaries: list[StoreSalesSummary], period: str) -> str:
    """Format sales summaries into source-opaque Slack-voice text."""
    if not summaries:
        return "No sales data available for that period."

    period_label = {
        "today": "Today so far",
        "yesterday": "Yesterday",
        "7d": "Last 7 days",
        "30d": "Last 30 days",
    }.get(period, period)

    lines = [f"*OSN sales — {period_label}:*", ""]
    total_rev = 0.0
    total_txns = 0
    total_refunds = 0.0

    for s in summaries:
        lines.append(
            f"*{s.store_name}* — ${s.revenue_usd:,.2f} net, "
            f"{s.transaction_count} txns, ${s.avg_ticket_usd:.2f} avg ticket"
            + (f", ${s.refund_total_usd:.2f} refunds" if s.refund_count > 0 else "")
        )
        total_rev += s.net_revenue_usd
        total_txns += s.transaction_count
        total_refunds += s.refund_total_usd

    if len(summaries) > 1:
        lines.append("")
        lines.append(
            f"*Portfolio total* — ${total_rev:,.2f} net, {total_txns} txns"
            + (f", ${total_refunds:.2f} refunds" if total_refunds > 0 else "")
        )

    return "\n".join(lines)


def format_inventory_for_llm(
    summaries: list[StoreInventorySummary],
    low_stock_only: bool = True,
) -> str:
    """Format inventory into source-opaque Slack-voice text."""
    if not summaries:
        return "No inventory data available."

    lines = ["*OSN inventory status:*", ""]
    for s in summaries:
        if low_stock_only and not s.low_stock_items:
            lines.append(f"*{s.store_name}* — all {s.total_items} SKUs adequately stocked")
        elif low_stock_only:
            lines.append(f"*{s.store_name}* — {len(s.low_stock_items)} low-stock SKUs:")
            for item in sorted(s.low_stock_items, key=lambda i: i.qty_on_hand):
                lines.append(
                    f"  • {item.name}"
                    + (f" (SKU: {item.sku})" if item.sku else "")
                    + f" — {item.qty_on_hand} left"
                )
        else:
            lines.append(f"*{s.store_name}* — {s.total_items} SKUs total, {len(s.low_stock_items)} low-stock")

    return "\n".join(lines)


def format_customer_trends_for_llm(summaries: list[StoreCustomerSummary]) -> str:
    """Format customer trend summaries into source-opaque Slack-voice text."""
    if not summaries:
        return "No customer data available."

    period = summaries[0].period if summaries else "30d"
    period_label = {
        "today": "today",
        "yesterday": "yesterday",
        "7d": "last 7 days",
        "30d": "last 30 days",
    }.get(period, period)

    lines = [f"*OSN customer trends — {period_label}:*", ""]
    for s in summaries:
        delta_str = ""
        if s.stats.pct_change is not None:
            arrow = "▲" if s.stats.pct_change >= 0 else "▼"
            delta_str = f" ({arrow}{abs(s.stats.pct_change):.1f}% vs prior period)"

        lines.append(
            f"*{s.store_name}* — {s.stats.new_customers} new customers{delta_str}, "
            f"{s.stats.total_transactions} transactions"
        )

    return "\n".join(lines)
