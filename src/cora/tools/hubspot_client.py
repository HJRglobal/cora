"""HubSpot CRM v3 client — read-only, single-token auth model.

Phase 2 #9 scope:
- Endpoint: POST /crm/v3/objects/deals/search (owner-filtered, incomplete deals)
- Two active pipelines on Starter: F3E Retail (2234421978), UFL Sponsorships (2242250445 — paused)
- Token: HUBSPOT_PRIVATE_APP_TOKEN from .env (Bearer auth)
- No write methods (read-only by design — user clicks deep link to edit in HubSpot)

Deep-link pattern: https://app.hubspot.com/contacts/{PORTAL_ID}/deal/{deal_id}
PORTAL_ID = 243870963 (HJR Global)
"""

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_PORTAL_ID = "243870963"  # HJR Global account
_TIMEOUT = 12.0
_DEFAULT_MAX_DEALS = 25

# Pipeline GIDs from founder OS memory
PIPELINE_F3E_RETAIL = "2234421978"
PIPELINE_UFL_SPONSORSHIPS = "2242250445"  # paused per UFL pause; included for completeness

# Stage GID → human-readable name cache (refreshed on first call per process)
_STAGE_NAME_CACHE: dict[str, str] = {}
_PIPELINE_NAME_CACHE: dict[str, str] = {}


class HubSpotClientError(Exception):
    """Raised when a HubSpot API call fails."""


def _token() -> str:
    val = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN", "")
    if not val:
        raise HubSpotClientError(
            "HUBSPOT_PRIVATE_APP_TOKEN not set in environment — HubSpot tool-use disabled"
        )
    return val


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }


def _refresh_pipeline_cache() -> None:
    """Fetch all deal pipelines + stages, cache name lookups."""
    global _STAGE_NAME_CACHE, _PIPELINE_NAME_CACHE
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(f"{_BASE}/crm/v3/pipelines/deals", headers=_headers())
    except httpx.RequestError as exc:
        raise HubSpotClientError(f"HubSpot network error (pipelines): {exc}") from exc

    if r.status_code != 200:
        raise HubSpotClientError(f"HubSpot pipelines {r.status_code}: {r.text[:200]}")

    pipelines = r.json().get("results", []) or []
    new_pipeline_cache: dict[str, str] = {}
    new_stage_cache: dict[str, str] = {}
    for pipeline in pipelines:
        p_id = pipeline.get("id", "")
        p_label = pipeline.get("label", "") or p_id
        new_pipeline_cache[p_id] = p_label
        for stage in pipeline.get("stages", []) or []:
            s_id = stage.get("id", "")
            s_label = stage.get("label", "") or s_id
            new_stage_cache[s_id] = s_label
    _STAGE_NAME_CACHE = new_stage_cache
    _PIPELINE_NAME_CACHE = new_pipeline_cache


def get_owner_deals(
    owner_id: str,
    pipeline_id: str | None = None,
    max_deals: int = _DEFAULT_MAX_DEALS,
) -> list[dict[str, Any]]:
    """Fetch open deals owned by a HubSpot owner.

    Filters to non-closed stages (closedwon / closedlost excluded). If pipeline_id is
    provided, scopes to that single pipeline; otherwise returns deals across all pipelines.
    """
    if not _STAGE_NAME_CACHE:
        _refresh_pipeline_cache()

    filters = [{"propertyName": "hubspot_owner_id", "operator": "EQ", "value": str(owner_id)}]
    if pipeline_id:
        filters.append({"propertyName": "pipeline", "operator": "EQ", "value": pipeline_id})

    body = {
        "filterGroups": [{"filters": filters}],
        "properties": [
            "dealname",
            "amount",
            "dealstage",
            "pipeline",
            "closedate",
            "createdate",
            "hubspot_owner_id",
            "hs_lastmodifieddate",
            # F3E custom properties (per founder OS memory)
            "f3e_channel",
            "f3e_geography",
            "f3e_product_lines",
            "f3e_monthly_volume_cases",
        ],
        "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}],
        "limit": max_deals,
    }

    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(
                f"{_BASE}/crm/v3/objects/deals/search", headers=_headers(), json=body
            )
    except httpx.RequestError as exc:
        raise HubSpotClientError(f"HubSpot network error: {exc}") from exc

    if r.status_code == 401:
        raise HubSpotClientError("HubSpot 401 — token invalid or revoked")
    if r.status_code == 403:
        raise HubSpotClientError(f"HubSpot 403 — token lacks scope for deal search")
    if r.status_code == 429:
        raise HubSpotClientError("HubSpot 429 — rate-limited; retry shortly")
    if r.status_code >= 500:
        raise HubSpotClientError(f"HubSpot {r.status_code} — upstream error: {r.text[:200]}")
    if r.status_code != 200:
        raise HubSpotClientError(f"HubSpot {r.status_code}: {r.text[:200]}")

    deals = r.json().get("results", []) or []
    # Drop closed-won / closed-lost (terminal states)
    open_deals = []
    for d in deals:
        stage_id = (d.get("properties") or {}).get("dealstage", "")
        stage_label = _STAGE_NAME_CACHE.get(stage_id, stage_id).lower()
        if "closed" in stage_label and ("won" in stage_label or "lost" in stage_label):
            continue
        open_deals.append(d)
    return open_deals


def _deal_url(deal_id: str) -> str:
    return f"https://app.hubspot.com/contacts/{_PORTAL_ID}/deal/{deal_id}"


def format_deals_for_llm(
    deals: list[dict[str, Any]],
    entity_scope: str | None = None,
    pipeline_filter_applied: bool = False,
) -> str:
    """Render deal list as a string suitable for a tool_result content block.

    Each deal name is wrapped in Slack mrkdwn hyperlink syntax `<url|dealname>`.
    Tool consumer (Claude) should preserve those links verbatim in user-facing replies.
    """
    if not deals:
        if entity_scope and pipeline_filter_applied:
            return (
                f"No open {entity_scope} deals found. User may have closed-won / closed-lost "
                f"deals only, or no deals assigned in this pipeline. Suggest checking HubSpot "
                f"directly: https://app.hubspot.com/contacts/{_PORTAL_ID}/"
            )
        return "No open HubSpot deals assigned to this user."

    header_prefix = f"Found {len(deals)} open HubSpot deal(s)"
    if entity_scope and pipeline_filter_applied:
        header = f"{header_prefix} in the {entity_scope} pipeline:"
    else:
        header = f"{header_prefix} across all pipelines:"

    lines = [header]
    lines.append(
        "(Deal names below are Slack-formatted hyperlinks — preserve the `<url|name>` "
        "syntax verbatim in your reply so the user can click through to edit in HubSpot.)"
    )

    for d in deals:
        deal_id = d.get("id", "")
        props = d.get("properties") or {}
        name = props.get("dealname") or "(no name)"
        amount = props.get("amount") or ""
        amount_str = f" ${float(amount):,.0f}" if amount else ""
        stage_id = props.get("dealstage", "")
        stage_label = _STAGE_NAME_CACHE.get(stage_id, stage_id)
        pipeline_id = props.get("pipeline", "")
        pipeline_label = _PIPELINE_NAME_CACHE.get(pipeline_id, pipeline_id)
        closedate = props.get("closedate") or "no close date"
        # closedate may be ms timestamp; trim to date if so
        if closedate and closedate.endswith("Z"):
            closedate = closedate[:10]  # YYYY-MM-DD prefix

        channel = props.get("f3e_channel") or ""
        product = props.get("f3e_product_lines") or ""
        geography = props.get("f3e_geography") or ""
        meta_bits = [b for b in [channel, product, geography] if b]
        meta_str = f" [{' / '.join(meta_bits)}]" if meta_bits else ""

        url = _deal_url(deal_id) if deal_id else ""
        name_with_link = f"<{url}|{name}>" if url else name

        lines.append(
            f"- [{stage_label}] {name_with_link}{amount_str} — pipeline: {pipeline_label} "
            f"— close: {closedate}{meta_str}"
        )

    return "\n".join(lines)
