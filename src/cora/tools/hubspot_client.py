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


# ── F3E Pipeline Summary ─────────────────────────────────────────────────────

# Stage ordering for F3E Retail pipeline (ascending funnel progress).
# Embedded from live HubSpot probe 2026-05-24 — overlay with _STAGE_NAME_CACHE on use.
_F3E_STAGE_ORDER: list[tuple[str, str]] = [
    ("3601439469", "Identify"),
    ("3601439470", "Outreach"),
    ("3672898248", "Sample Sent"),
    ("3672898250", "Qualified"),
    ("3672898249", "Proposal"),
    ("3604397771", "Negotiation"),
    ("3601439474", "Closed Won"),
    ("3601439475", "Closed Lost"),
]

# Stages where a deal is "hot" (action-required / approaching close)
_F3E_HOT_STAGE_IDS: frozenset[str] = frozenset(
    {"3672898250", "3672898249", "3604397771"}  # Qualified, Proposal, Negotiation
)
_F3E_CLOSED_WON_ID = "3601439474"
_F3E_CLOSED_LOST_ID = "3601439475"

# Owner short names for F3E Retail team (HubSpot owner_id str → display name)
_F3E_OWNER_SHORT: dict[str, str] = {
    "162944825": "Tommy",
    "160459333": "Harrison",
}


def _fetch_pipeline_deals(pipeline_id: str) -> list[dict[str, Any]]:
    """Fetch ALL deals in a pipeline, handling pagination."""
    if not _STAGE_NAME_CACHE:
        _refresh_pipeline_cache()

    body: dict[str, Any] = {
        "filterGroups": [
            {"filters": [{"propertyName": "pipeline", "operator": "EQ", "value": pipeline_id}]}
        ],
        "properties": [
            "dealname",
            "amount",
            "dealstage",
            "closedate",
            "hubspot_owner_id",
            "hs_lastmodifieddate",
            "deal_currency_code",
        ],
        "sorts": [{"propertyName": "amount", "direction": "DESCENDING"}],
        "limit": 100,
    }
    all_deals: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        if after:
            body["after"] = after
        try:
            with httpx.Client(timeout=_TIMEOUT) as c:
                r = c.post(
                    f"{_BASE}/crm/v3/objects/deals/search", headers=_headers(), json=body
                )
        except httpx.RequestError as exc:
            raise HubSpotClientError(f"HubSpot network error: {exc}") from exc
        if r.status_code == 401:
            raise HubSpotClientError("HubSpot 401 — token invalid or revoked")
        if r.status_code >= 400:
            raise HubSpotClientError(f"HubSpot {r.status_code}: {r.text[:200]}")
        data = r.json()
        all_deals.extend(data.get("results", []) or [])
        paging = data.get("paging") or {}
        next_after = (paging.get("next") or {}).get("after")
        if not next_after:
            break
        after = next_after
    return all_deals


def get_f3e_pipeline_summary_text() -> str:
    """Fetch the F3E Retail pipeline and return a structured summary string for Claude.

    Returns stage breakdown (count + $), hot list (Qualified/Proposal/Negotiation),
    owner split (Tommy vs Harrison), and recent closed deals.

    Source-opaque: no HubSpot IDs, no API references, 'as of [date]' freshness only.
    Deal names are wrapped in Slack mrkdwn <url|name> deep-link syntax (preserve verbatim).
    """
    from datetime import date as _date

    deals = _fetch_pipeline_deals(PIPELINE_F3E_RETAIL)
    today = _date.today().isoformat()

    # Stage label map: embedded first, overlaid with live cache
    stage_label: dict[str, str] = {sid: lbl for sid, lbl in _F3E_STAGE_ORDER}
    stage_label.update(_STAGE_NAME_CACHE)

    # Bucket deals by stage_id
    stage_buckets: dict[str, list[dict[str, Any]]] = {
        sid: [] for sid, _ in _F3E_STAGE_ORDER
    }
    for deal in deals:
        sid = (deal.get("properties") or {}).get("dealstage", "")
        stage_buckets.setdefault(sid, []).append(deal)

    def _amt(d: dict[str, Any]) -> float:
        raw = (d.get("properties") or {}).get("amount") or "0"
        try:
            return float(raw)
        except (ValueError, TypeError):
            return 0.0

    def _name(d: dict[str, Any]) -> str:
        return (d.get("properties") or {}).get("dealname") or "(unnamed)"

    def _owner_short(d: dict[str, Any]) -> str:
        oid = str((d.get("properties") or {}).get("hubspot_owner_id") or "")
        return _F3E_OWNER_SHORT.get(oid, "")

    def _closedate(d: dict[str, Any]) -> str:
        cd = (d.get("properties") or {}).get("closedate") or ""
        return cd[:10] if cd else ""

    def _deal_link(d: dict[str, Any]) -> str:
        did = str(d.get("id", ""))
        url = _deal_url(did) if did else ""
        nm = _name(d)
        return f"<{url}|{nm}>" if url else nm

    # Active stages (exclude terminal Closed Won/Lost)
    closed_ids = {_F3E_CLOSED_WON_ID, _F3E_CLOSED_LOST_ID}
    active_stage_ids = [sid for sid, _ in _F3E_STAGE_ORDER if sid not in closed_ids]
    active_deals = [d for sid in active_stage_ids for d in stage_buckets.get(sid, [])]
    active_count = len(active_deals)
    active_value = sum(_amt(d) for d in active_deals)

    won_deals = stage_buckets.get(_F3E_CLOSED_WON_ID, [])
    lost_deals = stage_buckets.get(_F3E_CLOSED_LOST_ID, [])

    lines: list[str] = [
        f"F3E Retail Pipeline — as of {today}",
        "",
        f"ACTIVE PIPELINE: {active_count} deal{'s' if active_count != 1 else ''} · ${active_value:,.0f} total value",
        "",
        "BY STAGE:",
    ]

    for sid, lbl in _F3E_STAGE_ORDER:
        if sid in closed_ids:
            continue
        bucket = stage_buckets.get(sid, [])
        if not bucket:
            continue
        count = len(bucket)
        val = sum(_amt(d) for d in bucket)
        lines.append(
            f"  {lbl:<14s}  {count:>2d} deal{'s' if count != 1 else ''}   ${val:>10,.0f}"
        )

    # Hot list: Qualified, Proposal, Negotiation — sorted by $ desc
    hot_deals = sorted(
        [d for sid in _F3E_HOT_STAGE_IDS for d in stage_buckets.get(sid, [])],
        key=_amt,
        reverse=True,
    )
    if hot_deals:
        lines += ["", "HOT LIST (Qualified / Proposal / Negotiation):"]
        for d in hot_deals:
            sid = (d.get("properties") or {}).get("dealstage", "")
            lbl = stage_label.get(sid, sid)
            amt = _amt(d)
            owner = _owner_short(d)
            cd = _closedate(d)
            parts = [f"${amt:,.0f}", lbl]
            if owner:
                parts.append(owner)
            if cd:
                parts.append(f"close {cd}")
            lines.append(f"  🔥 {_deal_link(d)} · {' · '.join(parts)}")

    # Owner split (active deals only)
    tommy_deals = [d for d in active_deals if _owner_short(d) == "Tommy"]
    harrison_deals = [d for d in active_deals if _owner_short(d) == "Harrison"]
    tommy_val = sum(_amt(d) for d in tommy_deals)
    harrison_val = sum(_amt(d) for d in harrison_deals)

    lines += ["", "OWNER SPLIT (active):"]
    lines.append(
        f"  Tommy:    {len(tommy_deals):>2d} deal{'s' if len(tommy_deals) != 1 else ''}   ${tommy_val:>10,.0f}"
    )
    if harrison_deals:
        lines.append(
            f"  Harrison: {len(harrison_deals):>2d} deal{'s' if len(harrison_deals) != 1 else ''}   ${harrison_val:>10,.0f}"
        )

    # Closed (all-time; pipeline is new enough that showing all is useful)
    if won_deals or lost_deals:
        lines.append("")
        lines.append("CLOSED:")
        for d in sorted(won_deals, key=_amt, reverse=True):
            amt = _amt(d)
            owner = _owner_short(d)
            owner_str = f" · {owner}" if owner else ""
            lines.append(f"  ✅ WON    {_deal_link(d)} · ${amt:,.0f}{owner_str}")
        for d in sorted(lost_deals, key=_amt, reverse=True):
            amt = _amt(d)
            owner = _owner_short(d)
            owner_str = f" · {owner}" if owner else ""
            lines.append(f"  ❌ LOST   {_deal_link(d)} · ${amt:,.0f}{owner_str}")

    lines += [
        "",
        "NOTE: Deal names above are Slack <url|name> hyperlinks — preserve verbatim in reply. "
        "Source-opaque: do not mention HubSpot, CRM, API, or any platform name.",
    ]
    return "\n".join(lines)


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
