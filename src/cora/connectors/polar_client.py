"""Polar Analytics connector -- reporting client.

Fetches F3 Energy ad/ecom performance data (spend, ROAS, CAC, CM waterfall,
subscriptions, channel + sub-brand breakdowns) and returns a normalized
``PolarReport`` (``tableData`` / ``totalData`` / ``deepLink`` / ``query_id``)
that the ads_client + ecom-brief layers consume by metric key.

Transport (two modes, selected by which credentials are present):

  1. MCP (the live path).  ``POLAR_API_KEY`` is the composite
     ``{client_id}|{client_secret}`` Polar MCP key (labelled "For clients that
     do not support OAuth" in the Polar app).  Verified empirically 2026-06-18:
     this credential authenticates as ``Authorization: Bearer {composite}``
     against the Polar **MCP** streamable-HTTP endpoint
     ``https://api.polaranalytics.com/mcp`` -- it is NOT a REST bearer and the
     OAuth token endpoint (``/oauth/token``) 404s ("OAuth flow not found"), so
     the key is used directly, never exchanged.  The MCP flow is
     ``initialize -> notifications/initialized -> tools/call get_context
     (-> conversation_id) -> tools/call generate_report``.  The Polar MCP
     ``generate_report`` tool wraps the same report engine and returns the same
     ``tableData``/``totalData``/``deepLink``/``query_id`` shape, so the result
     maps straight onto ``PolarReport`` and every caller is unchanged.

  2. REST + OAuth2 (preserved, dormant).  When explicit ``POLAR_CLIENT_ID`` +
     ``POLAR_CLIENT_SECRET`` are set, the client runs an OAuth2
     client-credentials exchange (``POLAR_OAUTH_URL``, default
     ``/oauth/token``) and POSTs to ``/api/v2/reports`` with the access token.
     Kept for the day Polar exposes a working OAuth/REST endpoint; it does NOT
     run for the composite MCP key.  A non-composite ``POLAR_API_KEY`` (no "|")
     is treated as a static REST bearer (also dormant today).

Behavioral contract (locked 2026-05-23):
  - Source-opaque: never log or surface the key, platform names, account IDs,
    or the Authorization header.  The Polar deep link IS passed through for
    creative assets (Option A doctrine) -- the ads_client layer decides when to
    surface it.
  - 15-minute in-memory report cache keyed by query fingerprint.
  - In-memory conversation cache (MCP) + OAuth token cache (REST), each with
    expiry; auto-refresh + single retry on failure.
  - Raises PolarConnectorError on any auth/API/parse failure so the caller can
    return UNKNOWN_RESPONSE / degrade fail-soft instead of surfacing a traceback.

Configuration (all optional -- bot boots without them, tools fail gracefully):
  POLAR_API_KEY        -- composite MCP key "{client_id}|{client_secret}"
                          (live path), OR a non-composite static REST bearer.
  POLAR_CLIENT_ID      -- OAuth2 client ID (enables the REST + OAuth path).
  POLAR_CLIENT_SECRET  -- OAuth2 client secret (enables the REST + OAuth path).
  POLAR_MCP_URL        -- Override MCP endpoint (default as above).
  POLAR_OAUTH_URL      -- Override OAuth token endpoint (default /oauth/token).
  POLAR_VIEW_ID        -- View ID for the F3 Energy brand filter (default below).
  POLAR_API_BASE_URL   -- Override REST base URL (default https://api.polaranalytics.com).
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

# Polar MCP streamable-HTTP endpoint -- the supported transport for the
# composite ("no-OAuth") key. Verified live 2026-06-18.
_MCP_ENDPOINT_DEFAULT = "https://api.polaranalytics.com/mcp"
_MCP_PROTOCOL_VERSION = "2025-06-18"

# F3 Energy brand view -- scopes all queries to F3 brand data only
_DEFAULT_VIEW_ID = "31499-mot5h6ya"

# Default attribution model for cross-channel queries
_DEFAULT_ATTRIBUTION_MODEL = "linear"

# Cache TTL: 15 minutes. Ad data refreshes frequently; cache prevents hammering
# the API on back-to-back Slack questions.
_CACHE_TTL_SECONDS = 900

# Reuse a Polar MCP conversation for this long before re-doing the
# initialize -> get_context handshake (the conversation_id is the only handle;
# the server itself is stateless -- no session id).
_CONVERSATION_TTL_SECONDS = 600

# HTTP timeout for Polar API calls
_HTTP_TIMEOUT_SECONDS = 30

# Refresh the OAuth token 60s before it actually expires
_TOKEN_EXPIRY_BUFFER_SECONDS = 60


# -------------------------------------------------------------------------
# Data structures
# -------------------------------------------------------------------------

@dataclass
class PolarReport:
    """Parsed result from a Polar generate_report call (MCP or REST)."""
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
    """Force-expire report cache + OAuth token + MCP conversation. For tests/ops."""
    _CACHE.clear()
    _invalidate_token()
    _invalidate_conversation()


# -------------------------------------------------------------------------
# OAuth2 token cache (REST path)
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
# MCP conversation cache
# -------------------------------------------------------------------------

# {"conversation_id": str, "expires_at": float}  -- monotonic clock
_CONV: dict[str, Any] = {}


def _invalidate_conversation() -> None:
    """Force-expire the cached MCP conversation_id."""
    _CONV.clear()


# -------------------------------------------------------------------------
# Config / credential helpers
# -------------------------------------------------------------------------

def _auth_mode() -> str:
    """Return the transport mode: 'oauth' | 'mcp' | 'static'.

    Priority:
      - explicit POLAR_CLIENT_ID + POLAR_CLIENT_SECRET -> 'oauth' (REST)
      - POLAR_API_KEY containing '|'                   -> 'mcp'   (composite key)
      - POLAR_API_KEY without '|'                      -> 'static' (REST bearer)

    Raises PolarConnectorError if no credentials are present.
    """
    client_id = os.environ.get("POLAR_CLIENT_ID", "").strip()
    client_secret = os.environ.get("POLAR_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        return "oauth"

    api_key = os.environ.get("POLAR_API_KEY", "").strip()
    if not api_key:
        raise PolarConnectorError(
            "No Polar credentials found. Set POLAR_API_KEY to the composite MCP key "
            "(client_id|client_secret) from app.polaranalytics.com, or set "
            "POLAR_CLIENT_ID + POLAR_CLIENT_SECRET for the OAuth/REST path."
        )
    if "|" in api_key:
        return "mcp"
    return "static"


def _client_credentials() -> tuple[str, str]:
    """Return (client_id, client_secret) for the REST/OAuth path.

      1. POLAR_CLIENT_ID + POLAR_CLIENT_SECRET (explicit OAuth creds), or
      2. POLAR_API_KEY without '|' -> static Bearer ("__static__", raw_key).

    A composite (piped) POLAR_API_KEY is an MCP key handled by the MCP transport,
    NOT here -- this raises for it so the REST path can never mis-exchange it.
    """
    client_id = os.environ.get("POLAR_CLIENT_ID", "").strip()
    client_secret = os.environ.get("POLAR_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        return client_id, client_secret

    api_key = os.environ.get("POLAR_API_KEY", "").strip()
    if not api_key:
        raise PolarConnectorError(
            "No Polar credentials found. Set POLAR_CLIENT_ID + POLAR_CLIENT_SECRET "
            "(OAuth), or POLAR_API_KEY (composite MCP key handled by the MCP transport)."
        )
    if "|" in api_key:
        raise PolarConnectorError(
            "POLAR_API_KEY is a composite MCP key; it is handled by the MCP transport, "
            "not the REST/OAuth path. _client_credentials() is for explicit "
            "POLAR_CLIENT_ID/SECRET or a non-composite static token only."
        )
    # Legacy: treat a non-composite POLAR_API_KEY as a static Bearer token
    return "__static__", api_key


def _mcp_bearer() -> str:
    """Return the composite MCP key, sent verbatim as the Bearer token.

    Verified: ``Authorization: Bearer {client_id}|{client_secret}`` (the whole
    composite, pipe included) is what the Polar MCP endpoint accepts.
    """
    api_key = os.environ.get("POLAR_API_KEY", "").strip()
    if "|" not in api_key:
        raise PolarConnectorError(
            "MCP transport requires a composite POLAR_API_KEY (client_id|client_secret)."
        )
    return api_key


def _get_bearer_token_any() -> str:
    """Return a valid REST Bearer token (OAuth-exchanged or static).

    Only used by the REST path (oauth / static modes). Composite MCP keys never
    reach here -- generate_report routes 'mcp' to the MCP transport first.
    """
    client_id, secret_or_key = _client_credentials()

    if client_id == "__static__":
        log.debug("Polar: using static Bearer token (legacy REST mode)")
        return secret_or_key

    # OAuth2 mode
    if _token_is_valid():
        return _TOKEN["access_token"]

    _exchange_token(client_id, secret_or_key)
    return _TOKEN["access_token"]


def _api_base_url() -> str:
    return os.environ.get("POLAR_API_BASE_URL", _API_BASE_URL_DEFAULT).rstrip("/")


def _mcp_url() -> str:
    return os.environ.get("POLAR_MCP_URL", _MCP_ENDPOINT_DEFAULT).strip()


def _view_id() -> str:
    return os.environ.get("POLAR_VIEW_ID", _DEFAULT_VIEW_ID).strip()


# -------------------------------------------------------------------------
# OAuth2 token exchange (REST path -- preserved, dormant)
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
# Shared response parsing
# -------------------------------------------------------------------------

def _parse_response(resp_json: dict, date_from: str, date_to: str,
                    metrics: list[str], dimensions: list[str]) -> PolarReport:
    """Parse a Polar report dict (REST body OR MCP tool result) into a PolarReport.

    Both transports return the same shape:
      {query_id, deepLink, tableData: [...], totalData: [...]}
    """
    query_id = resp_json.get("query_id", "") or ""
    deep_link = resp_json.get("deepLink", "") or resp_json.get("deeplink", "") or ""

    table_data = resp_json.get("tableData", [])
    total_list = resp_json.get("totalData", [])

    if not isinstance(table_data, list):
        raise PolarConnectorError(f"Unexpected tableData type: {type(table_data)}")

    if isinstance(total_list, list):
        total_data = total_list[0] if total_list else {}
    elif isinstance(total_list, dict):
        total_data = total_list
    else:
        total_data = {}

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


# -------------------------------------------------------------------------
# REST transport (OAuth / static modes -- preserved, dormant)
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


def _generate_report_via_rest(
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
) -> PolarReport:
    """REST + OAuth2/static transport (preserved; dormant unless explicit
    POLAR_CLIENT_ID/SECRET or a non-composite POLAR_API_KEY are set)."""
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

    return _parse_response(resp_json, date_from, date_to, metrics, dimensions)


# -------------------------------------------------------------------------
# MCP transport (composite key -- the live path)
# -------------------------------------------------------------------------

def _mcp_headers() -> dict:
    """Headers for every MCP request. The Bearer is the composite key.

    NEVER logged -- source-opacity. Streamable HTTP requires the dual Accept.
    """
    return {
        "Authorization": f"Bearer {_mcp_bearer()}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }


def _mcp_post(payload: dict) -> httpx.Response:
    """POST one JSON-RPC payload to the Polar MCP endpoint."""
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            return client.post(_mcp_url(), headers=_mcp_headers(), json=payload)
    except httpx.TimeoutException as exc:
        raise PolarConnectorError(f"Polar MCP request timed out: {exc}") from exc
    except httpx.RequestError as exc:
        raise PolarConnectorError(f"Polar MCP request failed: {exc}") from exc


def _raise_for_mcp_http(response: httpx.Response) -> None:
    """Raise PolarConnectorError on a non-OK MCP HTTP status.

    The response body never contains the key (it is in the request header), so
    including a short snippet is source-safe.
    """
    if response.status_code == 401:
        raise PolarConnectorError(
            "Polar MCP returned 401 -- check POLAR_API_KEY (composite MCP key) is valid."
        )
    if response.status_code == 403:
        raise PolarConnectorError(
            "Polar MCP returned 403 -- credential lacks access to this workspace."
        )
    if response.status_code not in (200, 202):
        raise PolarConnectorError(
            f"Polar MCP returned HTTP {response.status_code}: {(response.text or '')[:200]}"
        )


def _parse_mcp_body(response: httpx.Response) -> dict:
    """Return the JSON-RPC object from an MCP response (SSE or plain JSON)."""
    text = response.text or ""
    try:
        ctype = response.headers.get("content-type", "") or ""
    except Exception:
        ctype = ""

    is_sse = (
        "event-stream" in ctype
        or text.lstrip().startswith("event:")
        or text.lstrip().startswith("data:")
        or "\ndata:" in text
    )
    if is_sse:
        obj = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("data:"):
                chunk = stripped[len("data:"):].strip()
                if chunk and chunk != "[DONE]":
                    try:
                        obj = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
        if obj is None:
            raise PolarConnectorError("Could not parse Polar MCP SSE response.")
        return obj

    try:
        return response.json()
    except Exception as exc:
        raise PolarConnectorError(
            f"Could not parse Polar MCP JSON response: {exc}"
        ) from exc


def _extract_tool_result(obj: dict) -> dict:
    """From a JSON-RPC tools/call object, return the structured tool result dict.

    Prefers ``structuredContent``; falls back to JSON in ``content[0].text``.
    Raises PolarConnectorError on a JSON-RPC error or a tool ``isError`` result.
    """
    if not isinstance(obj, dict):
        raise PolarConnectorError("Polar MCP response was not a JSON object.")
    if "error" in obj:
        raise PolarConnectorError(f"Polar MCP JSON-RPC error: {str(obj['error'])[:200]}")

    result = obj.get("result")
    if not isinstance(result, dict):
        raise PolarConnectorError("Polar MCP response missing 'result'.")

    if result.get("isError"):
        content = result.get("content") or []
        msg = content[0].get("text", "") if content and isinstance(content[0], dict) else ""
        raise PolarConnectorError(f"Polar MCP tool error: {str(msg)[:200]}")

    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured:
        return structured

    content = result.get("content") or []
    if content and isinstance(content[0], dict):
        text = content[0].get("text", "")
        try:
            parsed = json.loads(text)
        except Exception as exc:
            raise PolarConnectorError(
                f"Could not parse Polar MCP tool result JSON: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise PolarConnectorError("Polar MCP tool result was not a JSON object.")
        return parsed

    raise PolarConnectorError("Polar MCP tool result was empty.")


def _mcp_tool_call(name: str, arguments: dict, call_id: int = 3) -> dict:
    """Invoke an MCP tool and return its structured result dict."""
    payload = {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    response = _mcp_post(payload)
    _raise_for_mcp_http(response)
    return _extract_tool_result(_parse_mcp_body(response))


def _mcp_handshake_and_context() -> str:
    """initialize -> initialized -> get_context. Returns a fresh conversation_id.

    The server is stateless (no session id), so the conversation_id from
    get_context is the only handle generate_report needs.
    """
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "cora", "version": "1.0"},
        },
    }
    init_resp = _mcp_post(init_payload)
    _raise_for_mcp_http(init_resp)
    init_obj = _parse_mcp_body(init_resp)
    if isinstance(init_obj, dict) and "error" in init_obj:
        raise PolarConnectorError(
            f"Polar MCP initialize error: {str(init_obj['error'])[:200]}"
        )

    # Required notification after initialize (fire-and-forget, 202).
    notif_resp = _mcp_post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    _raise_for_mcp_http(notif_resp)

    ctx = _mcp_tool_call(
        "get_context",
        {"initialQuestion": "Cora automated report"},
        call_id=2,
    )
    conversation_id = ctx.get("conversation_id")
    if not conversation_id:
        raise PolarConnectorError("Polar MCP get_context returned no conversation_id.")
    return conversation_id


def _get_conversation_id() -> str:
    """Return a cached conversation_id, or run the handshake to mint a fresh one."""
    now = time.monotonic()
    cached = _CONV.get("conversation_id")
    if cached and now < _CONV.get("expires_at", 0.0):
        return cached
    conversation_id = _mcp_handshake_and_context()
    _CONV["conversation_id"] = conversation_id
    _CONV["expires_at"] = now + _CONVERSATION_TTL_SECONDS
    return conversation_id


def _mcp_report_args(
    conversation_id: str,
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
    """Map the connector's call shape onto the MCP generate_report arg shape
    (comma-strings + JSON-strings)."""
    ordering_str = ",".join(
        f"{o.get('columnKey', '')}:{o.get('direction', 'DESC')}"
        for o in (ordering or [])
        if o.get("columnKey")
    )
    return {
        "conversation_id": conversation_id,
        "metrics": ",".join(metrics),
        "dimensions": ",".join(dimensions),
        "dateRangeFrom": date_from,
        "dateRangeTo": date_to,
        "granularity": granularity,
        "ordering": ordering_str,
        "settings": json.dumps(settings or {}),
        "rules": json.dumps(rules or {}),
        "metricRules": json.dumps(metric_rules or {}),
        "views": _view_id(),
        "limit": str(limit),
        "reflexion": "Cora automated report (entity-scoped, source-opaque).",
    }


def _generate_report_via_mcp(
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
) -> PolarReport:
    """MCP transport: get_context -> generate_report, with one refresh+retry.

    A failure (stale conversation, transport blip, tool error) triggers a single
    conversation refresh + retry before surfacing PolarConnectorError.
    """
    def _attempt(conversation_id: str) -> dict:
        args = _mcp_report_args(
            conversation_id, metrics, dimensions, date_from, date_to,
            granularity, settings, rules, metric_rules, ordering, limit,
        )
        return _mcp_tool_call("generate_report", args)

    conversation_id = _get_conversation_id()
    try:
        report_dict = _attempt(conversation_id)
    except PolarConnectorError as first_exc:
        log.info("Polar MCP report failed (%s) -- refreshing conversation and retrying once",
                 str(first_exc)[:120])
        _invalidate_conversation()
        conversation_id = _get_conversation_id()
        report_dict = _attempt(conversation_id)

    return _parse_response(report_dict, date_from, date_to, metrics, dimensions)


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

    Parameters mirror the Polar generate_report tool. Metrics and dimensions use
    Polar key names (e.g. total_marketing_spend).

    Transport is auto-selected: a composite POLAR_API_KEY uses the MCP endpoint
    (the live path); explicit POLAR_CLIENT_ID/SECRET (or a non-composite static
    key) use the REST endpoint.

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

    mode = _auth_mode()
    log.info(
        "Fetching Polar report (transport=%s): metrics=%s dimensions=%s %s->%s",
        mode, metrics, dimensions, date_from, date_to,
    )

    if mode == "mcp":
        report = _generate_report_via_mcp(
            metrics, dimensions, date_from, date_to, granularity,
            settings, rules, metric_rules, ordering, limit,
        )
    else:
        report = _generate_report_via_rest(
            metrics, dimensions, date_from, date_to, granularity,
            settings, rules, metric_rules, ordering, limit,
        )

    _cache_set(ck, report)

    log.info(
        "Polar report loaded: %d rows, query_id=%s",
        len(report.table_data),
        report.query_id,
    )
    return report
