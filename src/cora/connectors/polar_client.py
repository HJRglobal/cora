"""Polar Analytics connector — reporting API client.

Calls the Polar Analytics REST API (/api/v2/reports) to fetch ad performance
data across all connected channels (Meta, TikTok, Google Ads, Amazon Ads,
Shopify, Polar Pixel, Recharge).

Auth: POLAR_API_KEY env var. Generate at:
  app.polaranalytics.com → Settings → API → Create API Key

Behavioral contract (locked 2026-05-23):
  - Source-opaque: never log or surface platform names, account IDs, or deep links
    in the rendered answer layer. Deep links ARE passed through for creative assets
    (Option A doctrine) — the ads_client layer decides when to surface them.
  - 15-minute in-memory cache keyed by query fingerprint
  - Raises PolarConnectorError on any auth/API/parse failure so the caller
    can return UNKNOWN_RESPONSE instead of surfacing a traceback

Configuration (all optional — bot boots without them, tools gracefully fail):
  POLAR_API_KEY        — API key from app.polaranalytics.com settings
  POLAR_VIEW_ID        — view ID for F3 Energy brand filter (default: 31499-mot5h6ya)
  POLAR_API_BASE_URL   — override API base URL (default: https://api.polaranalytics.com)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

_API_BASE_URL_DEFAULT = "https://api.polaranalytics.com"
_REPORT_ENDPOINT = "/api/v2/reports"

# F3 Energy brand view — scopes all queries to F3 brand data only
_DEFAULT_VIEW_ID = "31499-mot5h6ya"

# Default attribution model for cross-channel queries
_DEFAULT_ATTRIBUTION_MODEL = "linear"

# Cache TTL: 15 minutes. Ad data refreshes frequently; cache prevents hammering
# the API on back-to-back Slack questions.
_CACHE_TTL_SECONDS = 900

# HTTP timeout for Polar API calls
_HTTP_TIMEOUT_SECONDS = 30


# ────────────────────────────────────────────────────────────────────────────
# Data structures
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class PolarReport:
    """Parsed result from a Polar Analytics generate_report call."""
    query_id: str
    table_data: list[dict[str, Any]]  # one dict per row
    total_data: dict[str, Any]        # totals row (single dict)
    deep_link: str                    # Polar app deep link (for Option A creative links)
    date_from: str                    # YYYY-MM-DD
    date_to: str                      # YYYY-MM-DD
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────────────
# Error type
# ────────────────────────────────────────────────────────────────────────────

class PolarConnectorError(Exception):
    """Raised when the Polar API call or response parse fails."""


# ────────────────────────────────────────────────────────────────────────────
# In-memory cache
# ────────────────────────────────────────────────────────────────────────────

# {fingerprint: (fetched_at_unix, PolarReport)}
_CACHE: dict[str, tuple[float, PolarReport]] = {}


def _cache_key(
    metrics: list[str],
    dimensions: list[str],
    date_from: str,
    date_to: str,
    granularity: str,
    rules: dict,
    metric_rules: dict,
) -> str:
    """Stable SHA-256 fingerprint for a query shape."""
    payload = json.dumps(
        {
            "m": sorted(metrics),
            "d": sorted(dimensions),
            "from": date_from,
            "to": date_to,
            "g": granularity,
            "r": rules,
            "mr": metric_rules,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _cache_get(key: str) -> Optional[PolarReport]:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    fetched_at, report = entry
    if time.monotonic() - fetched_at > _CACHE_TTL_SECONDS:
        del _CACHE[key]
        return None
    return report


def _cache_set(key: str, report: PolarReport) -> None:
    _CACHE[key] = (time.monotonic(), report)


def invalidate_cache() -> None:
    """Force-expire entire cache. Useful for tests."""
    _CACHE.clear()


# ────────────────────────────────────────────────────────────────────────────
# Config helpers
# ────────────────────────────────────────────────────────────────────────────

def _api_key() -> str:
    val = os.environ.get("POLAR_API_KEY", "").strip()
    if not val:
        raise PolarConnectorError(
            "POLAR_API_KEY not set — Polar Analytics connector disabled. "
            "Generate a key at app.polaranalytics.com → Settings → API."
        )
    return val


def _api_base_url() -> str:
    return os.environ.get("POLAR_API_BASE_URL", _API_BASE_URL_DEFAULT).rstrip("/")


def _view_id() -> str:
    return os.environ.get("POLAR_VIEW_ID", _DEFAULT_VIEW_ID).strip()


# ────────────────────────────────────────────────────────────────────────────
# API call
# ────────────────────────────────────────────────────────────────────────────

def _build_request_body(
    metrics: list[str],
    dimensions: list[str],
    date_from: str,
    date_to: str,
    granularity: str,
    settings: dict,
    rules: dict,
    metric_rules: dict,
    ordering: list[dict],
    limit: int,
) -> dict:
    body: dict[str, Any] = {
        "views": [_view_id()],
        "metrics": metrics,
        "dimensions": dimensions,
        "dateRangeFrom": date_from,
        "dateRangeTo": date_to,
        "granularity": granularity,
        "settings": settings,
        "rules": rules,
        "metricRules": metric_rules,
        "limit": limit,
    }
    if ordering:
        body["ordering"] = ordering
    return body


def _parse_response(resp_json: dict, date_from: str, date_to: str,
                    metrics: list[str], dimensions: list[str]) -> PolarReport:
    """Parse the Polar API JSON response into a PolarReport."""
    query_id = resp_json.get("query_id", "")
    deep_link = resp_json.get("deepLink", "")

    table_data = resp_json.get("tableData", [])
    total_list = resp_json.get("totalData", [])
    total_data = total_list[0] if total_list else {}

    if not isinstance(table_data, list):
        raise PolarConnectorError(f"Unexpected tableData type: {type(table_data)}")

    return PolarReport(
        query_id=query_id,
        table_data=table_data,
        total_data=total_data,
        deep_link=deep_link,
        date_from=date_from,
        date_to=date_to,
        metrics=metrics,
        dimensions=dimensions,
    )


# ────────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────────

def generate_report(
    metrics: list[str],
    dimensions: list[str],
    date_from: str,
    date_to: str,
    granularity: str = "none",
    settings: Optional[dict] = None,
    rules: Optional[dict] = None,
    metric_rules: Optional[dict] = None,
    ordering: Optional[list[dict]] = None,
    limit: int = 100,
) -> PolarReport:
    """Fetch a report from Polar Analytics.

    Parameters mirror the Polar `generate_report` MCP tool.
    Metrics and dimensions use the exact Polar key names (e.g.
    'total_marketing_spend', 'custom_5621').

    Caches results for _CACHE_TTL_SECONDS (15 min).
    Raises PolarConnectorError on auth/API/parse failure.
    """
    settings = settings or {"attribution_model": _DEFAULT_ATTRIBUTION_MODEL}
    rules = rules or {}
    metric_rules = metric_rules or {}
    ordering = ordering or []

    ck = _cache_key(metrics, dimensions, date_from, date_to, granularity, rules, metric_rules)
    cached = _cache_get(ck)
    if cached is not None:
        log.debug("Returning cached Polar report (fingerprint redacted)")
        return cached

    api_key = _api_key()  # raises PolarConnectorError if missing
    url = _api_base_url() + _REPORT_ENDPOINT
    body = _build_request_body(
        metrics=metrics,
        dimensions=dimensions,
        date_from=date_from,
        date_to=date_to,
        granularity=granularity,
        settings=settings,
        rules=rules,
        metric_rules=metric_rules,
        ordering=ordering,
        limit=limit,
    )

    log.info(
        "Fetching Polar report: metrics=%s dimensions=%s %s→%s",
        metrics, dimensions, date_from, date_to,
    )

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=body,
            )
    except httpx.TimeoutException as exc:
        raise PolarConnectorError(f"Polar API request timed out: {exc}") from exc
    except httpx.RequestError as exc:
        raise PolarConnectorError(f"Polar API request failed: {exc}") from exc

    if response.status_code == 401:
        raise PolarConnectorError(
            "Polar API returned 401 — check POLAR_API_KEY is valid and not expired."
        )
    if response.status_code == 403:
        raise PolarConnectorError(
            "Polar API returned 403 — key may lack reporting permissions."
        )
    if response.status_code != 200:
        raise PolarConnectorError(
            f"Polar API returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        resp_json = response.json()
    except Exception as exc:
        raise PolarConnectorError(
            f"Failed to parse Polar API JSON response: {exc}"
        ) from exc

    report = _parse_response(resp_json, date_from, date_to, metrics, dimensions)
    _cache_set(ck, report)

    log.info(
        "Polar report loaded: %d rows, query_id=%s",
        len(report.table_data),
        report.query_id,
    )
    return report
