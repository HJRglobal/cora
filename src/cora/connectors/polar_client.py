"""Polar Analytics connector -- reporting API client.

Calls the Polar Analytics REST API (/api/v2/reports) to fetch ad performance
data across all connected channels (Meta, TikTok, Google Ads, Amazon Ads,
Shopify, Polar Pixel, Recharge).

Auth: OAuth2 client-credentials flow. Reads POLAR_CLIENT_ID + POLAR_CLIENT_SECRET
from env (preferred). Falls back to parsing POLAR_API_KEY as
"{client_id}|{client_secret}" (the Polar MCP key format). If POLAR_API_KEY
contains no "|", treats it as a static Bearer token for legacy compatibility.

Token exchange endpoint: POLAR_OAUTH_URL env var, default
  https://api.polaranalytics.com/oauth/token

Behavioral contract (locked 2026-05-23):
  - Source-opaque: never log or surface platform names, account IDs, or deep links
    in the rendered answer layer. Deep links ARE passed through for creative assets
    (Option A doctrine) -- the ads_client layer decides when to surface them.
  - 15-minute in-memory cache keyed by query fingerprint (report data)
  - In-memory token cache with expiry; auto-refresh on 401; 60s buffer before expiry
  - Raises PolarConnectorError on any auth/API/parse failure so the caller
    can return UNKNOWN_RESPONSE instead of surfacing a traceback

Configuration (all optional -- bot boots without them, tools gracefully fail):
  POLAR_CLIENT_ID      -- OAuth2 client ID (preferred)
  POLAR_CLIENT_SECRET  -- OAuth2 client secret (preferred)
  POLAR_API_KEY        -- Legacy: "{client_id}|{client_secret}" pipe-delimited,
                          OR static Bearer token if no "|" present
  POLAR_OAUTH_URL      -- Override token endpoint (default as above)
  POLAR_VIEW_ID        -- View ID for F3 Energy brand filter (default: 31499-mot5h6ya)
  POLAR_API_BASE_URL   -- Override API base URL (default: https://api.polaranalytics.com)
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

# -------------------------------------------------------------------------
# Constants
# -------------------------------------------------------------------------

_API_BASE_URL_DEFAULT = "https://api.polaranalytics.com"
_REPORT_ENDPOINT = "/api/v2/reports"
_OAUTH_TOKEN_URL_DEFAULT = "https://api.polaranalytics.com/oauth/token"

# F3 Energy brand view -- scopes all queries to F3 brand data only
_DEFAULT_VIEW_ID = "31499-mot5h6ya"

# Default attribution model for cross-channel queries
_DEFAULT_ATTRIBUTION_MODEL = "linear"

# Cache TTL: 15 minutes. Ad data refreshes frequently; cache prevents hammering
# the API on back-to-back Slack questions.
_CACHE_TTL_SECONDS = 900

# HTTP timeout for Polar API calls
_HTTP_TIMEOUT_SECONDS = 30

# Refresh the OAuth token 60s before it actually expires
_TOKEN_EXPIRY_BUFFER_SECONDS = 60


# -------------------------------------------------------------------------
# Data structures
# -------------------------------------------------------------------------

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


# -------------------------------------------------------------------------
# Error type
# -------------------------------------------------------------------------

class PolarConnectorError(Exception):
    """Raised when the Polar API call or response parse fails."""


# -------------------------------------------------------------------------
# In-memory report cache
# -------------------------------------------------------------------------

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
    """Force-expire entire report cache and OAuth token. Useful for tests."""
    _CACHE.clear()
    _invalidate_token()


# -------------------------------------------------------------------------
# OAuth2 token cache
# -------------------------------------------------------------------------

# {"access_token": str, "expires_at": float}  -- monotonic clock
_TOKEN: dict[str, Any] = {}


def _invalidate_token() -> None:
    """Force-expire the cached OAuth token (e.g. on 401)."""
    _TOKEN.clear()


def _token_is_valid() -> bool:
    if not _TOKEN:
        return False
    return time.monotonic() < _TOKEN["expires_at"]


# -------------------------------------------------------------------------
# OAuth2 token exchange
# -------------------------------------------------------------------------

def _exchange_token(client_id: str, client_secret: str) -> None:
    """POST to the OAuth token endpoint and populate the _TOKEN cache."""
    oauth_url = os.environ.get("POLAR_OAUTH_URL", _OAUTH_TOKEN_URL_DEFAULT).strip()

    log.info("Exchanging Polar OAuth token via %s", oauth_url)

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            response = client.post(
                oauth_url,
                json={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
    except httpx.TimeoutException as exc:
        raise PolarConnectorError(
            f"Polar OAuth token exchange timed out: {exc}"
        ) from exc
    except httpx.RequestError as exc:
        raise PolarConnectorError(
            f"Polar OAuth token exchange request failed: {exc}"
        ) from exc

    if response.status_code == 401:
        raise PolarConnectorError(
            "Polar OAuth token exchange returned 401 -- "
            "check POLAR_CLIENT_ID and POLAR_CLIENT_SECRET are correct."
        )
    if response.status_code not in (200, 201):
        raise PolarConnectorError(
            f"Polar OAuth token exchange returned HTTP {response.status_code}: "
            f"{response.text[:200]}"
        )

    try:
        data = response.json()
    except Exception as exc:
        raise PolarConnectorError(
            f"Failed to parse Polar OAuth token response: {exc}"
        ) from exc

    access_token = data.get("access_token")
    if not access_token:
        raise PolarConnectorError(
            f"Polar OAuth token response missing access_token: {str(data)[:200]}"
        )

    expires_in = data.get("expires_in", 3600)
    try:
        expires_in = int(expires_in)
    except (TypeError, ValueError):
        expires_in = 3600

    _TOKEN["access_token"] = access_token
    _TOKEN["expires_at"] = time.monotonic() + expires_in - _TOKEN_EXPIRY_BUFFER_SECONDS

    log.info(
        "Polar OAuth token obtained (expires_in=%ds, buffer=%ds)",
        expires_in,
        _TOKEN_EXPIRY_BUFFER_SECONDS,
    )


# -------------------------------------------------------------------------
# Config helpers
# -------------------------------------------------------------------------

def _client_credentials() -> tuple[str, str]:
    """Return (client_id, client_secret) from env.

    Priority:
      1. POLAR_CLIENT_ID + POLAR_CLIENT_SECRET (explicit, preferred)
      2. POLAR_API_KEY as "client_id|client_secret" (Polar MCP key format)
      3. POLAR_API_KEY as static Bearer token -- returned as ("__static__", raw_key)

    Raises PolarConnectorError if no credentials are available.
    """
    client_id = os.environ.get("POLAR_CLIENT_ID", "").strip()
    client_secret = os.environ.get("POLAR_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        return client_id, client_secret

    api_key = os.environ.get("POLAR_API_KEY", "").strip()
    if not api_key:
        raise PolarConnectorError(
            "No Polar credentials found. Set POLAR_CLIENT_ID + POLAR_CLIENT_SECRET "
            "(preferred), or POLAR_API_KEY as client_id|client_secret. "
            "Generate at app.polaranalytics.com -> Settings -> API."
        )

    if "|" in api_key:
        parts = api_key.split("|", 1)
        return parts[0].strip(), parts[1].strip()

    # Legacy: treat POLAR_API_KEY as a static Bearer token
    return "__static__", api_key


def _get_bearer_token_any() -> str:
    """Return a valid Bearer token, handling both OAuth and legacy static modes."""
    client_id, secret_or_key = _client_credentials()

    if client_id == "__static__":
        # Legacy static Bearer key -- no token exchange
        log.debug("Polar: using static Bearer token (legacy mode)")
        return secret_or_key

    # OAuth2 mode
    if _token_is_valid():
        return _TOKEN["access_token"]

    _exchange_token(client_id, secret_or_key)
    return _TOKEN["access_token"]


def _api_base_url() -> str:
    return os.environ.get("POLAR_API_BASE_URL", _API_BASE_URL_DEFAULT).rstrip("/")


def _view_id() -> str:
    return os.environ.get("POLAR_VIEW_ID", _DEFAULT_VIEW_ID).strip()


# -------------------------------------------------------------------------
# API call helpers
# -------------------------------------------------------------------------

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


def _do_report_request(url: str, body: dict, bearer_token: str) -> httpx.Response:
    """Execute a single POST /api/v2/reports request."""
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            return client.post(
                url,
                headers={
                    "Authorization": f"Bearer {bearer_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=body,
            )
    except httpx.TimeoutException as exc:
        raise PolarConnectorError(f"Polar API request timed out: {exc}") from exc
    except httpx.RequestError as exc:
        raise PolarConnectorError(f"Polar API request failed: {exc}") from exc


# -------------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------------

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

    Parameters mirror the Polar generate_report MCP tool.
    Metrics and dimensions use Polar key names (e.g. total_marketing_spend).

    Auth: OAuth2 client-credentials (auto-refreshed). Falls back to static
    Bearer token if POLAR_API_KEY is set without a pipe character.

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
        "Fetching Polar report: metrics=%s dimensions=%s %s->%s",
        metrics, dimensions, date_from, date_to,
    )

    # First attempt
    bearer = _get_bearer_token_any()
    response = _do_report_request(url, body, bearer)

    # 401: invalidate token + retry once (OAuth mode only)
    if response.status_code == 401:
        client_id, _ = _client_credentials()
        if client_id != "__static__":
            log.info("Polar API returned 401 -- invalidating token and retrying")
            _invalidate_token()
            bearer = _get_bearer_token_any()
            response = _do_report_request(url, body, bearer)

    # Error handling
    if response.status_code == 401:
        raise PolarConnectorError(
            "Polar API returned 401 after token refresh -- "
            "check credentials are valid and have reporting permissions."
        )
    if response.status_code == 403:
        raise PolarConnectorError(
            "Polar API returned 403 -- credentials may lack reporting permissions."
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
