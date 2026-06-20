"""Shopify connector for F3 Energy DTC store.

Reads orders and inventory from the Shopify Admin REST API on-demand.
5-minute in-memory cache prevents hammering the API on back-to-back questions.

Store: f3energy.myshopify.com (one store, three domains: F3Energy.com / F3Pure.com / F3Mood.com)

Auth: SHOPIFY_F3E_ACCESS_TOKEN + SHOPIFY_F3E_STORE env vars.

Behavioral contract (mirrors gsheets_financials.py):
  - Source-opaque: never surface store URLs, token values, or "Shopify" unless asked
  - All monetary values returned as float USD
  - Raises ShopifyConnectorError on auth/API failure
  - AZ timezone (America/Phoenix, UTC-7, no DST) for period calculations
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

_API_VERSION = "2024-10"
_CACHE_TTL_SECONDS = 300
_PAGE_SIZE = 250
_AZ_UTC_OFFSET = -7  # America/Phoenix, no DST

LOW_STOCK_THRESHOLD = 10
VALID_PERIODS = ("today", "yesterday", "7d", "30d")


class ShopifyConnectorError(Exception):
    """Raised when the Shopify API is unreachable or returns an error."""


class ShopifyConfigError(ShopifyConnectorError):
    """Raised when required env vars are missing."""


@dataclass
class TopProduct:
    title: str
    quantity_sold: int
    revenue_usd: float


@dataclass
class SalesSummary:
    period: str
    order_count: int
    gross_revenue_usd: float
    discounts_usd: float
    refunds_usd: float
    net_revenue_usd: float
    avg_order_value_usd: float
    top_products: list[TopProduct] = field(default_factory=list)


@dataclass
class InventoryVariant:
    product_title: str
    variant_title: str
    sku: str
    qty_on_hand: int
    low_stock: bool
    product_type: str = ""


@dataclass
class LocationSKU:
    """Per-SKU inventory at a specific Shopify fulfillment location."""
    product_title: str
    sku: str
    available: int


# ---------------------------------------------------------------------------
# Cache
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
    _cache.clear()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _store_config() -> tuple[str, str]:
    """Return (store_domain, access_token). Raises ShopifyConfigError if not set."""
    store = os.getenv("SHOPIFY_F3E_STORE", "")
    token = os.getenv("SHOPIFY_F3E_ACCESS_TOKEN", "")
    if not store or not token:
        raise ShopifyConfigError(
            "SHOPIFY_F3E_STORE and/or SHOPIFY_F3E_ACCESS_TOKEN not set."
        )
    return store, token


def _base_url(store: str) -> str:
    return f"https://{store}/admin/api/{_API_VERSION}"


def _headers(token: str) -> dict[str, str]:
    return {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# Period helpers (AZ time, UTC-7, no DST)
# ---------------------------------------------------------------------------

def _az_now() -> datetime:
    return datetime.now(tz=timezone(timedelta(hours=_AZ_UTC_OFFSET)))


def _period_to_iso(period: str) -> tuple[str, str]:
    """Return (start_iso, end_iso) in UTC for a named period based on AZ clock."""
    now = _az_now()
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
        raise ShopifyConfigError(
            f"Unknown period {period!r}. Valid: {', '.join(VALID_PERIODS)}"
        )
    utc = timezone.utc
    return (
        start.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        end.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_paginated(url: str, token: str, params: Optional[dict] = None) -> list[dict]:
    """Paginate through a Shopify list endpoint using Link-header cursor pagination."""
    headers = _headers(token)
    results: list[dict] = []
    current_url = url
    current_params: Optional[dict] = dict(params or {})

    while True:
        try:
            resp = requests.get(
                current_url,
                headers=headers,
                params=current_params,
                timeout=15,
            )
        except requests.RequestException as exc:
            raise ShopifyConnectorError(f"Network error: {exc}") from exc

        if resp.status_code == 401:
            raise ShopifyConnectorError(
                "Auth failed (HTTP 401). Check SHOPIFY_F3E_ACCESS_TOKEN."
            )
        if not resp.ok:
            raise ShopifyConnectorError(
                f"Shopify API error {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        list_key = next((k for k, v in data.items() if isinstance(v, list)), None)
        if list_key:
            results.extend(data[list_key])

        # Cursor pagination via Link header
        next_url = None
        for part in resp.headers.get("Link", "").split(","):
            part = part.strip()
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
                break

        if not next_url:
            break
        current_url = next_url
        current_params = None  # params are embedded in the cursor URL

    return results


# ---------------------------------------------------------------------------
# Public API: Sales Pulse
# ---------------------------------------------------------------------------

def get_sales_pulse(period: str = "today") -> SalesSummary:
    """Return DTC sales summary for the F3E Shopify store over a period."""
    cache_key = f"sales:{period}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    store, token = _store_config()
    start_iso, end_iso = _period_to_iso(period)

    orders = _get_paginated(
        f"{_base_url(store)}/orders.json",
        token,
        params={
            "status": "any",
            "financial_status": "paid",
            "created_at_min": start_iso,
            "created_at_max": end_iso,
            "limit": _PAGE_SIZE,
            "fields": "id,total_price,subtotal_price,total_discounts,refunds,line_items",
        },
    )

    gross = 0.0
    discounts = 0.0
    refunds = 0.0
    product_sales: dict[str, list] = {}  # title -> [qty, revenue]

    for order in orders:
        gross += float(order.get("total_price") or 0)
        discounts += float(order.get("total_discounts") or 0)

        for refund in order.get("refunds") or []:
            for rt in refund.get("refund_line_items") or []:
                refunds += float(rt.get("subtotal") or 0)

        for item in order.get("line_items") or []:
            title = item.get("title") or "Unknown"
            qty = int(item.get("quantity") or 0)
            price = float(item.get("price") or 0) * qty
            if title in product_sales:
                product_sales[title][0] += qty
                product_sales[title][1] += price
            else:
                product_sales[title] = [qty, price]

    order_count = len(orders)
    net = gross - refunds
    aov = gross / order_count if order_count > 0 else 0.0

    top_raw = sorted(product_sales.items(), key=lambda x: x[1][1], reverse=True)[:5]
    top_products = [
        TopProduct(title=t, quantity_sold=v[0], revenue_usd=round(v[1], 2))
        for t, v in top_raw
    ]

    result = SalesSummary(
        period=period,
        order_count=order_count,
        gross_revenue_usd=round(gross, 2),
        discounts_usd=round(discounts, 2),
        refunds_usd=round(refunds, 2),
        net_revenue_usd=round(net, 2),
        avg_order_value_usd=round(aov, 2),
        top_products=top_products,
    )
    _cache_set(cache_key, result)
    log.info(
        "shopify sales_pulse period=%s orders=%d gross=%.2f net=%.2f",
        period, order_count, gross, net,
    )
    return result


# ---------------------------------------------------------------------------
# Public API: Inventory
# ---------------------------------------------------------------------------

def get_inventory_status(
    low_stock_threshold: int = LOW_STOCK_THRESHOLD,
) -> list[InventoryVariant]:
    """Return variant-level inventory for all F3E products.

    Correctness invariants (WS11): on-hand is clamped to >= 0 (Shopify's
    `inventory_quantity` goes NEGATIVE on oversell -- an accounting flag, never a
    real "units left"), and variant rows are DEDUPED by Shopify variant id so a
    paginated overlap or a product listed twice can't inflate the count.
    """
    cache_key = f"inventory:{low_stock_threshold}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    store, token = _store_config()
    products = _get_paginated(
        f"{_base_url(store)}/products.json",
        token,
        params={"limit": _PAGE_SIZE, "fields": "id,title,product_type,variants"},
    )

    by_variant_id: dict[str, InventoryVariant] = {}
    for product in products:
        product_title = product.get("title") or "Unknown product"
        product_type = (product.get("product_type") or "").strip()
        for v in product.get("variants") or []:
            variant_title = (v.get("title") or "").strip()
            if variant_title.lower() in ("default title", "default"):
                variant_title = ""
            sku = v.get("sku") or ""
            # Clamp: Shopify oversell can report a negative inventory_quantity.
            qty = max(0, int(v.get("inventory_quantity") or 0))
            vid = str(v.get("id") or f"{product_title}|{variant_title}|{sku}")
            by_variant_id[vid] = InventoryVariant(
                product_title=product_title,
                variant_title=variant_title,
                sku=sku,
                qty_on_hand=qty,
                low_stock=(qty <= low_stock_threshold),
                product_type=product_type,
            )

    variants = list(by_variant_id.values())
    _cache_set(cache_key, variants)

    total_units = sum(v.qty_on_hand for v in variants)
    unique_skus = len({v.sku for v in variants if v.sku})
    zero_count = sum(1 for v in variants if v.qty_on_hand == 0)
    low_count = sum(1 for v in variants if v.low_stock)
    # Consistency guard: total_units==0 must coincide with every variant at 0.
    # (Mathematically guaranteed post-clamp; the guard catches a future regression
    # that re-introduces negatives or a bad aggregation.)
    if (total_units == 0) != (zero_count == len(variants)):
        log.warning(
            "shopify inventory INCONSISTENT: total_units=%d zero=%d/%d variants",
            total_units, zero_count, len(variants),
        )
    log.info(
        "shopify inventory variants=%d skus=%d units=%d zero=%d low_stock=%d threshold=%d",
        len(variants), unique_skus, total_units, zero_count, low_count, low_stock_threshold,
    )
    return variants


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_sales_for_llm(summary: SalesSummary) -> str:
    """Format sales summary into source-opaque Slack-voice text."""
    period_label = {
        "today": "Today so far",
        "yesterday": "Yesterday",
        "7d": "Last 7 days",
        "30d": "Last 30 days",
    }.get(summary.period, summary.period)

    lines = [f"*F3E DTC sales -- {period_label}:*", ""]
    lines.append(f"*Orders:* {summary.order_count}")
    lines.append(f"*Gross revenue:* ${summary.gross_revenue_usd:,.2f}")
    if summary.discounts_usd > 0:
        lines.append(f"*Discounts:* ${summary.discounts_usd:,.2f}")
    if summary.refunds_usd > 0:
        lines.append(f"*Refunds:* ${summary.refunds_usd:,.2f}")
    lines.append(f"*Net revenue:* ${summary.net_revenue_usd:,.2f}")
    if summary.order_count > 0:
        lines.append(f"*AOV:* ${summary.avg_order_value_usd:.2f}")

    if summary.top_products:
        lines.append("")
        lines.append("*Top products:*")
        for p in summary.top_products:
            lines.append(f"  - {p.title}: {p.quantity_sold} units, ${p.revenue_usd:,.2f}")

    return "\n".join(lines)


def format_inventory_for_llm(
    variants: list[InventoryVariant],
    low_stock_only: bool = True,
) -> str:
    """Format inventory into source-opaque Slack-voice text."""
    if not variants:
        return "No inventory data available."

    # SKUs != variants: one product has several variants (size/flavor), so report
    # both counts + total units on hand so the snapshot is self-consistent (WS11).
    unique_skus = len({v.sku for v in variants if v.sku}) or len(variants)
    total_units = sum(v.qty_on_hand for v in variants)
    summary = f"{unique_skus} SKUs / {len(variants)} variants, {total_units:,} units on hand"

    if low_stock_only:
        flagged = [v for v in variants if v.low_stock]
        if not flagged:
            return f"*F3E inventory:* {summary} -- all adequately stocked."
        flagged_skus = len({v.sku for v in flagged if v.sku}) or len(flagged)
        lines = [
            f"*F3E inventory -- low stock ({flagged_skus} SKUs, {len(flagged)} variants):* _{summary}_",
            "",
        ]
        for v in sorted(flagged, key=lambda x: x.qty_on_hand):
            label = v.product_title
            if v.variant_title:
                label += f" ({v.variant_title})"
            entry = f"  - {label}: {v.qty_on_hand} units left"
            if v.sku:
                entry += f" [SKU: {v.sku}]"
            lines.append(entry)
    else:
        lines = [f"*F3E inventory ({summary}):*", ""]
        by_product: dict[str, list[InventoryVariant]] = {}
        for v in variants:
            by_product.setdefault(v.product_title, []).append(v)
        for product_title, pvariants in sorted(by_product.items()):
            lines.append(f"*{product_title}*")
            for v in sorted(pvariants, key=lambda x: x.variant_title or ""):
                label = v.variant_title or "Default"
                flag = " [LOW]" if v.low_stock else ""
                lines.append(f"  - {label}: {v.qty_on_hand} units{flag}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Location-aware inventory (Nimbl real-time, etc.)
# ---------------------------------------------------------------------------


def _get_locations() -> dict[str, int]:
    """Return {location_name_lower: location_id} for all active Shopify locations.

    Cached for the standard TTL (5 min) -- locations rarely change but
    using the shared cache keeps invalidation simple.
    """
    cache_key = "locations"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    store, token = _store_config()
    try:
        resp = requests.get(
            f"{_base_url(store)}/locations.json",
            headers=_headers(token),
            timeout=15,
        )
    except requests.RequestException as exc:
        raise ShopifyConnectorError(f"Network error fetching locations: {exc}") from exc

    if resp.status_code == 401:
        raise ShopifyConnectorError(
            "Auth failed (HTTP 401) fetching locations. Check SHOPIFY_F3E_ACCESS_TOKEN."
        )
    if not resp.ok:
        raise ShopifyConnectorError(
            f"Shopify locations API error {resp.status_code}: {resp.text[:200]}"
        )

    locations = resp.json().get("locations", [])
    result: dict[str, int] = {}
    for loc in locations:
        if loc.get("active") and loc.get("name") and loc.get("id"):
            result[loc["name"].lower()] = loc["id"]
    _cache_set(cache_key, result)
    log.info("shopify locations loaded: %d active", len(result))
    return result


def _infer_brand_from_title(product_title: str) -> str:
    """Infer F3 brand bucket (Energy / Mood / Pure) from a Shopify product title.

    NOTE: this is a brand *bucketer*, not a beverage filter -- it defaults
    un-matched titles to "Energy", so it would mislabel apparel as a brand.
    Use is_beverage_product() to separate beverages from apparel/merch.
    """
    t = product_title.lower()
    if "pure" in t:
        return "Pure"
    if "mood" in t:
        return "Mood"
    return "Energy"


# Beverage vs. apparel/merch classification for the F3E Shopify catalog.
#
# The live catalog (verified 2026-06-18) tags every beverage with a product_type
# ("Pure Drink" / "Mood Drink" / "Energy Drink" / "Energy & Mood Drink" /
# "Energy") and leaves apparel/merch (tees, hats, pullovers) product_type BLANK.
# So product_type is the primary, reliable signal. The title heuristic is only a
# fallback for products whose product_type is blank or unrecognized (e.g. if
# someone forgets to set it), and it is merch-exclude-FIRST so a merch item that
# happens to carry a beverage word in its title (e.g. "F3 Energy Koozie") is
# still excluded. Word-boundary matching avoids substring false-hits ("cap" in
# "escapade", "hat" in "what", "pack" in "backpack").
#
# NOTE: "bottle" is deliberately NOT a merch token. Merch is checked BEFORE
# beverage, so a token that also appears in beverage names ("F3 Pure Sparkling
# Water Bottle", "Energy 16oz Bottle") would silently FALSE-EXCLUDE a real
# low-stock beverage -- the cardinal failure this filter exists to prevent.
# F3 ships cans today (caught by "cans?" in the beverage pattern) and has no
# bottle/drinkware merch; if a drinkware merch line ever needs excluding, give
# it a dedicated product_type, never a beverage-vocabulary-overlapping title word.
_MERCH_PATTERN = re.compile(
    r"\b(?:t[\s-]?shirt|tee|shirt|pullover|hoodie|sweatshirt|crewneck|jacket|"
    r"joggers?|shorts|socks?|hat|cap|beanie|apparel|merch|sticker|koozie|mug|"
    r"tumbler|gear)\b"
)
_BEVERAGE_PATTERN = re.compile(
    r"\b(?:drink|energy|mood|pure|beverage|variety|\d*[\s-]?pack|cans?)\b"
)


def is_beverage_product(product_type: str, product_title: str = "") -> bool:
    """True if a Shopify product is an F3 *beverage* (not apparel/merch).

    product_type is the primary signal (every live beverage carries one;
    apparel/merch leaves it blank). product_title is a merch-exclude-first
    fallback used only when product_type is blank or unrecognized.
    """
    pt = (product_type or "").strip().lower()
    if _MERCH_PATTERN.search(pt):
        return False
    if _BEVERAGE_PATTERN.search(pt):
        return True
    # Blank or unrecognized product_type -> title fallback (merch wins ties).
    title = (product_title or "").lower()
    if _MERCH_PATTERN.search(title):
        return False
    return bool(_BEVERAGE_PATTERN.search(title))


def get_inventory_by_location(
    location_name: str,
    brand: Optional[str] = None,
) -> list[LocationSKU]:
    """Return live inventory at a specific Shopify fulfillment location.

    Looks up the location by name (case-insensitive, partial match OK), then
    fetches inventory_levels for that location and cross-references products /
    variants to return per-SKU available counts.

    Args:
        location_name: Location name fragment -- e.g. "nimbl" or "Nimbl 3PL".
        brand: Optional filter -- "Pure", "Mood", or "Energy".

    Returns:
        List of LocationSKU sorted by product_title.

    Raises:
        ShopifyConnectorError: if the location is unknown / ambiguous or API fails.
        ShopifyConfigError: if env vars are missing.
    """
    cache_key = f"location_inventory:{location_name.lower()}:{(brand or '').lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    store, token = _store_config()

    # Resolve location ID (exact match first, then partial)
    locations = _get_locations()
    needle = location_name.lower()
    location_id: Optional[int] = locations.get(needle)
    if location_id is None:
        matches = {k: v for k, v in locations.items() if needle in k}
        if len(matches) == 0:
            raise ShopifyConnectorError(
                f"Location {location_name!r} not found. "
                f"Known locations: {sorted(locations.keys())}"
            )
        if len(matches) > 1:
            raise ShopifyConnectorError(
                f"Location {location_name!r} is ambiguous -- matches: {sorted(matches.keys())}. "
                f"Use a more specific name."
            )
        location_id = list(matches.values())[0]

    # Fetch inventory levels for this location
    inv_levels = _get_paginated(
        f"{_base_url(store)}/inventory_levels.json",
        token,
        params={"location_ids": str(location_id), "limit": _PAGE_SIZE},
    )
    inv_by_item: dict[int, int] = {}
    for lvl in inv_levels:
        if lvl.get("inventory_item_id") is not None:
            # Clamp: Shopify oversell can report negative `available` (WS11).
            inv_by_item[int(lvl["inventory_item_id"])] = max(0, int(lvl.get("available") or 0))

    if not inv_by_item:
        empty: list[LocationSKU] = []
        _cache_set(cache_key, empty)
        return empty

    # Fetch products to map inventory_item_id -> display title + SKU
    products = _get_paginated(
        f"{_base_url(store)}/products.json",
        token,
        params={"limit": _PAGE_SIZE, "fields": "id,title,variants"},
    )
    item_map: dict[int, tuple[str, str]] = {}  # item_id -> (display_title, sku)
    for product in products:
        product_title = product.get("title") or "Unknown"
        for v in product.get("variants") or []:
            item_id = v.get("inventory_item_id")
            if not item_id:
                continue
            sku = v.get("sku") or ""
            variant_title = (v.get("title") or "").strip()
            if variant_title.lower() in ("default title", "default", ""):
                display_title = product_title
            else:
                display_title = f"{product_title} - {variant_title}"
            item_map[int(item_id)] = (display_title, sku)

    # Build result, optionally filtered by brand
    brand_lower = (brand or "").lower()
    skus: list[LocationSKU] = []
    for item_id, available in inv_by_item.items():
        if item_id not in item_map:
            continue
        display_title, sku = item_map[item_id]
        inferred_brand = _infer_brand_from_title(display_title)
        if brand_lower and inferred_brand.lower() != brand_lower:
            continue
        skus.append(LocationSKU(
            product_title=display_title,
            sku=sku,
            available=available,
        ))

    skus.sort(key=lambda x: x.product_title)
    _cache_set(cache_key, skus)
    log.info(
        "shopify location_inventory location_id=%s location=%r brand=%s skus=%d",
        location_id, location_name, brand or "ALL", len(skus),
    )
    return skus


def format_location_inventory_for_llm(
    skus: list[LocationSKU],
    location_name: str,
    brand: Optional[str] = None,
) -> str:
    """Format location-specific inventory into source-opaque Slack-voice text."""
    brand_label = f"F3 {brand.capitalize()}" if brand else "F3E"
    loc_display = location_name.capitalize()

    if not skus:
        return (
            f"*{brand_label} inventory at {loc_display}:* "
            f"No stock on hand at this location."
        )

    lines = [f"*{brand_label} inventory at {loc_display} (live):*", ""]

    # Group by inferred brand
    by_brand: dict[str, list[LocationSKU]] = {}
    for s in skus:
        b = _infer_brand_from_title(s.product_title)
        by_brand.setdefault(b, []).append(s)

    for b in ("Energy", "Mood", "Pure"):
        b_skus = by_brand.get(b)
        if not b_skus:
            continue
        if not brand:  # multi-brand: show brand header
            lines.append(f"*F3 {b}*")
        for s in b_skus:
            flag = "\U0001f6a8" if s.available <= 10 else "\u26a0\ufe0f" if s.available <= 50 else "\u2705"
            lines.append(f"{flag} {s.product_title}: *{s.available:,}* units")
        if not brand:
            lines.append("")

    lines.append("_Live data. Units = individual cans._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GraphQL helper (used by photoroom_client and other connectors that need
# mutations or queries not available via the REST Admin API)
# ---------------------------------------------------------------------------


def graphql(mutation: str, variables: dict) -> dict:
    """
    Execute a Shopify Admin GraphQL query or mutation.

    Returns the parsed JSON response body as a dict.
    Raises ShopifyConnectorError on HTTP error or Shopify-level errors.
    """
    store, token = _store_config()
    url = f"https://{store}/admin/api/{_API_VERSION}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }
    payload = {"query": mutation, "variables": variables}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
    except requests.RequestException as exc:
        raise ShopifyConnectorError(f"GraphQL network error: {exc}") from exc

    if resp.status_code == 401:
        raise ShopifyConnectorError(
            "Shopify GraphQL auth failed (HTTP 401). Check SHOPIFY_F3E_ACCESS_TOKEN."
        )
    if not resp.ok:
        raise ShopifyConnectorError(
            f"Shopify GraphQL HTTP {resp.status_code}: {resp.text[:300]}"
        )

    body = resp.json()
    if "errors" in body:
        msg = "; ".join(
            e.get("message", str(e)) for e in body["errors"]
        )
        raise ShopifyConnectorError(f"Shopify GraphQL errors: {msg}")

    return body
