"""HubSpot CRM v3 client — read-only, single-token auth model.

Phase 2 #9 scope:
- Endpoint: POST /crm/v3/objects/deals/search (owner-filtered, incomplete deals)
- Four pipelines: F3E Retail, UFL Sponsorships, OSN, BDM (new account 246351746)
- Token: HUBSPOT_PRIVATE_APP_TOKEN from .env (Bearer auth)
- No write methods (read-only by design — user clicks deep link to edit in HubSpot)

Deep-link pattern: https://app.hubspot.com/contacts/{PORTAL_ID}/deal/{deal_id}
PORTAL_ID = 246351746 (HJR Global — new account, migrated 2026-05-30)
"""

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_PORTAL_ID = "246351746"  # HJR Global — new account (migrated 2026-05-30)
_TIMEOUT = 12.0
_DEFAULT_MAX_DEALS = 25

# Pipeline IDs — new account (portal 246351746), created via HubSpot UI 2026-05-30
PIPELINE_F3E_RETAIL   = "2313722582"
PIPELINE_UFL_OSN_BDM  = "default"    # renamed from "Sales Pipeline"; covers UFL, OSN, BDM deals

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

# Stage ordering for F3E Retail pipeline — new account stage IDs (probed 2026-05-30).
# Identify/Outreach/Sample Sent IDs resolved at runtime via _refresh_pipeline_cache();
# hot-stage IDs and terminal IDs are hardcoded for fast filtering.
_F3E_STAGE_ORDER: list[tuple[str, str]] = [
    ("",           "Identify"),     # ID resolved from live cache
    ("",           "Outreach"),     # ID resolved from live cache
    ("",           "Sample Sent"),  # ID resolved from live cache
    ("3760235204", "Qualified"),
    ("3760204497", "Proposal"),
    ("3760235205", "Negotiation"),
    ("3760235206", "Closed Won"),
    ("3760235207", "Closed Lost"),
]

_F3E_HOT_STAGE_IDS: frozenset[str] = frozenset(
    {"3760235204", "3760204497", "3760235205"}  # Qualified, Proposal, Negotiation
)
_F3E_CLOSED_WON_ID  = "3760235206"
_F3E_CLOSED_LOST_ID = "3760235207"

# Owner short names for F3E Retail team (confirmed same IDs in new account)
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


def get_deals_by_pipeline(pipeline_id: str) -> list[dict[str, Any]]:
    """Public alias for _fetch_pipeline_deals — returns raw deal objects from HubSpot API."""
    return _fetch_pipeline_deals(pipeline_id)


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

    # Build resolved stage order.  _F3E_STAGE_ORDER has empty-string IDs for
    # stages whose IDs were not known at migration time (Identify, Outreach,
    # Sample Sent).  After _fetch_pipeline_deals -> _refresh_pipeline_cache,
    # _STAGE_NAME_CACHE is populated with {stage_id: label} from the live portal.
    # Build a reverse (name -> id) map and fill in the empty slots.
    _name_to_id: dict[str, str] = {v.lower(): k for k, v in _STAGE_NAME_CACHE.items()}
    resolved_order: list[tuple[str, str]] = []
    for sid, lbl in _F3E_STAGE_ORDER:
        if not sid:
            sid = _name_to_id.get(lbl.lower(), "")
        resolved_order.append((sid, lbl))

    # Stage label map: id -> display name
    stage_label: dict[str, str] = {sid: lbl for sid, lbl in resolved_order if sid}
    stage_label.update(_STAGE_NAME_CACHE)

    # Bucket deals by stage_id
    stage_buckets: dict[str, list[dict[str, Any]]] = {
        sid: [] for sid, _ in resolved_order if sid
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
    active_stage_ids = [sid for sid, _ in resolved_order if sid and sid not in closed_ids]
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

    for sid, lbl in resolved_order:
        if not sid or sid in closed_ids:
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


def search_distributor_company(name: str) -> dict | None:
    """Search HubSpot for a company matching `name` and return an enrichment dict.

    Pulls company record + primary contact + associated deals. Used to enrich
    sales deck generation with CRM context before Claude writes slide content.

    Returns None (never raises) if HubSpot is unconfigured, the company isn't
    found, or any API call fails — callers proceed with the info they have.
    """
    try:
        token = _token()
    except HubSpotClientError:
        return None

    hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Search companies by name (CONTAINS_TOKEN = case-insensitive substring match)
    search_body = {
        "filterGroups": [{"filters": [{
            "propertyName": "name",
            "operator": "CONTAINS_TOKEN",
            "value": name,
        }]}],
        "properties": [
            "name", "website", "industry", "phone",
            "city", "state", "description", "numberofemployees",
        ],
        "limit": 3,
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(f"{_BASE}/crm/v3/objects/companies/search", headers=hdrs, json=search_body)
        if r.status_code != 200:
            log.warning("HubSpot company search HTTP %s for %r", r.status_code, name)
            return None
        results = r.json().get("results", []) or []
        if not results:
            return None
        company = results[0]
    except Exception as exc:
        log.warning("HubSpot company search failed for %r: %s", name, exc)
        return None

    company_id = company.get("id", "")
    props = company.get("properties") or {}

    enrichment: dict = {
        "company_id": company_id,
        "name": props.get("name") or name,
        "website": props.get("website") or "",
        "industry": props.get("industry") or "",
        "phone": props.get("phone") or "",
        "city": props.get("city") or "",
        "state": props.get("state") or "",
        "description": props.get("description") or "",
        "num_employees": props.get("numberofemployees") or "",
        "primary_contact_name": "",
        "primary_contact_title": "",
        "primary_contact_email": "",
        "open_deals": [],
        "hubspot_url": f"https://app.hubspot.com/contacts/{_PORTAL_ID}/company/{company_id}",
    }

    if not company_id:
        return enrichment

    # Fetch primary contact via company → contacts association
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(
                f"{_BASE}/crm/v3/objects/companies/{company_id}/associations/contacts",
                headers=hdrs,
            )
        if r.status_code == 200:
            contact_refs = r.json().get("results", []) or []
            if contact_refs:
                contact_id = contact_refs[0].get("id", "")
                if contact_id:
                    with httpx.Client(timeout=_TIMEOUT) as c:
                        rc = c.get(
                            f"{_BASE}/crm/v3/objects/contacts/{contact_id}",
                            headers=hdrs,
                            params={"properties": "firstname,lastname,jobtitle,email"},
                        )
                    if rc.status_code == 200:
                        cp = rc.json().get("properties") or {}
                        first = (cp.get("firstname") or "").strip()
                        last = (cp.get("lastname") or "").strip()
                        enrichment["primary_contact_name"] = f"{first} {last}".strip()
                        enrichment["primary_contact_title"] = cp.get("jobtitle") or ""
                        enrichment["primary_contact_email"] = cp.get("email") or ""
    except Exception as exc:
        log.warning("HubSpot contact fetch failed for company %s: %s", company_id, exc)

    # Fetch associated deals (surface existing F3E relationship if any)
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(
                f"{_BASE}/crm/v3/objects/companies/{company_id}/associations/deals",
                headers=hdrs,
            )
        if r.status_code == 200:
            deal_refs = (r.json().get("results", []) or [])[:5]
            for dr in deal_refs:
                deal_id = dr.get("id", "")
                if not deal_id:
                    continue
                with httpx.Client(timeout=_TIMEOUT) as c:
                    rd = c.get(
                        f"{_BASE}/crm/v3/objects/deals/{deal_id}",
                        headers=hdrs,
                        params={"properties": "dealname,dealstage,amount,closedate"},
                    )
                if rd.status_code == 200:
                    dp = rd.json().get("properties") or {}
                    deal_name = dp.get("dealname") or ""
                    stage_id = dp.get("dealstage") or ""
                    stage = _STAGE_NAME_CACHE.get(stage_id, stage_id)
                    amount = dp.get("amount") or ""
                    amount_str = f" · ${float(amount):,.0f}" if amount else ""
                    enrichment["open_deals"].append(f"{deal_name} — {stage}{amount_str}")
    except Exception as exc:
        log.warning("HubSpot deal fetch failed for company %s: %s", company_id, exc)

    log.info(
        "HubSpot enrichment found company=%r contact=%r deals=%d",
        enrichment["name"], enrichment["primary_contact_name"], len(enrichment["open_deals"]),
    )
    return enrichment


# ── Email engagement write methods ──────────────────────────────────────────────

def search_contact_by_email(email: str) -> dict | None:
    """Search HubSpot for a contact with matching email. Returns first result or None."""
    try:
        token = _token()
    except HubSpotClientError:
        return None

    hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email.lower()}]}],
        "properties": ["firstname", "lastname", "email", "company"],
        "limit": 1,
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(f"{_BASE}/crm/v3/objects/contacts/search", headers=hdrs, json=body)
        if r.status_code != 200:
            return None
        results = r.json().get("results", []) or []
        return results[0] if results else None
    except Exception as exc:
        log.warning("HubSpot contact search failed for %r: %s", email, exc)
        return None


def get_contact_deal_ids(contact_id: str) -> list[str]:
    """Return deal IDs associated with a contact via v3 associations."""
    try:
        token = _token()
    except HubSpotClientError:
        return []

    hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(
                f"{_BASE}/crm/v3/objects/contacts/{contact_id}/associations/deals",
                headers=hdrs,
            )
        if r.status_code != 200:
            return []
        return [str(ref.get("id", "")) for ref in r.json().get("results", []) or [] if ref.get("id")]
    except Exception as exc:
        log.warning("HubSpot deal association fetch failed for contact %s: %s", contact_id, exc)
        return []


def log_email_engagement(
    from_email: str,
    to_emails: list[str],
    subject: str,
    body_text: str,
    timestamp_ms: int,
    direction: str,
    owner_id: str,
    contact_ids: list[str],
    deal_ids: list[str],
) -> str:
    """Log an email engagement via v1 engagements API. Returns engagement ID or '' on failure.

    direction: "INBOUND" (email received by rep) or "OUTBOUND" (email sent by rep).
    Uses the v1 engagements API because it handles contact+deal associations atomically.
    """
    token = _token()
    hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    body: dict = {
        "engagement": {
            "active": True,
            "type": "EMAIL",
            "timestamp": timestamp_ms,
        },
        "associations": {
            "contactIds": [int(c) for c in contact_ids if c],
            "companyIds": [],
            "dealIds": [int(d) for d in deal_ids if d],
            "ownerIds": [int(owner_id)] if owner_id else [],
        },
        "metadata": {
            "from": {"email": from_email},
            "to": [{"email": e} for e in to_emails if e],
            "subject": subject or "(no subject)",
            "text": body_text[:8000] if body_text else "",
            "html": "",
            "direction": direction,
        },
    }

    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(f"{_BASE}/engagements/v1/engagements", headers=hdrs, json=body)
        if r.status_code not in (200, 201):
            raise HubSpotClientError(
                f"HubSpot email engagement {r.status_code}: {r.text[:200]}"
            )
        result = r.json()
        engagement_id = str((result.get("engagement") or {}).get("id", ""))
        log.info(
            "HubSpot email logged: id=%s  subject=%r  contacts=%s  deals=%s",
            engagement_id, subject[:40], contact_ids, deal_ids,
        )
        return engagement_id
    except HubSpotClientError:
        raise
    except Exception as exc:
        raise HubSpotClientError(f"log_email_engagement failed: {exc}") from exc


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


# ---------------------------------------------------------------------------
# Company write helpers
# ---------------------------------------------------------------------------

def find_company_by_name(name: str) -> str | None:
    """Search HubSpot for a company by exact name. Returns company ID or None."""
    hdrs = _headers()
    body = {
        "filterGroups": [{
            "filters": [{"propertyName": "name", "operator": "EQ", "value": name}]
        }],
        "properties": ["name", "address"],
        "limit": 1,
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(f"{_BASE}/crm/v3/objects/companies/search", headers=hdrs, json=body)
        if r.status_code != 200:
            return None
        results = r.json().get("results", []) or []
        return str(results[0]["id"]) if results else None
    except Exception as exc:
        log.warning("HubSpot company search failed for %r: %s", name, exc)
        return None


def create_company(
    *,
    name: str,
    address: str = "",
    city: str = "",
    state: str = "",
    zip_code: str = "",
    phone: str = "",
    industry: str = "",
) -> str:
    """Create a HubSpot company. Returns the new company ID."""
    hdrs = _headers()
    props: dict[str, str] = {"name": name}
    if address:
        props["address"] = address
    if city:
        props["city"] = city
    if state:
        props["state"] = state
    if zip_code:
        props["zip"] = zip_code
    if phone:
        props["phone"] = phone
    if industry:
        props["industry"] = industry

    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(
                f"{_BASE}/crm/v3/objects/companies",
                headers=hdrs,
                json={"properties": props},
            )
        if r.status_code in (200, 201):
            return str(r.json().get("id", ""))
        raise HubSpotClientError(f"create_company {r.status_code}: {r.text[:200]}")
    except HubSpotClientError:
        raise
    except Exception as exc:
        raise HubSpotClientError(f"create_company network error: {exc}") from exc


def associate_company_to_deal(company_id: str, deal_id: str) -> None:
    """Associate a company with a deal via v4 associations API."""
    hdrs = _headers()
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            c.put(
                f"{_BASE}/crm/v4/objects/companies/{company_id}/associations/deals/{deal_id}",
                headers=hdrs,
                json=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 5}],
            )
    except Exception as exc:
        log.warning("HubSpot company-deal association failed: %s", exc)


# ---------------------------------------------------------------------------
# Write helpers — LinkedIn Spy → HubSpot pipeline
# ---------------------------------------------------------------------------

# F3E Retail pipeline first stage ("Identify") — confirmed 2026-05-31
_F3E_STAGE_IDENTIFY = "3760235201"


def find_contact_by_linkedin_url(linkedin_url: str) -> str | None:
    """Search HubSpot contacts by LinkedIn URL. Returns contact ID or None."""
    if not linkedin_url:
        return None
    hdrs = _headers()
    body = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "hs_linkedin_url",
                "operator": "EQ",
                "value": linkedin_url,
            }]
        }],
        "properties": ["hs_linkedin_url", "firstname", "lastname"],
        "limit": 1,
    }
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(f"{_BASE}/crm/v3/objects/contacts/search", headers=hdrs, json=body)
        if r.status_code != 200:
            return None
        results = r.json().get("results", []) or []
        return str(results[0]["id"]) if results else None
    except Exception as exc:
        log.warning("HubSpot linkedin search failed for %s: %s", linkedin_url, exc)
        return None


def create_contact(
    *,
    first_name: str,
    last_name: str,
    job_title: str,
    company: str,
    linkedin_url: str,
) -> str:
    """Create a HubSpot contact. Returns the new contact ID."""
    hdrs = _headers()
    props: dict[str, str] = {}
    if first_name:
        props["firstname"] = first_name
    if last_name:
        props["lastname"] = last_name
    if job_title:
        props["jobtitle"] = job_title
    if company:
        props["company"] = company
    if linkedin_url:
        props["hs_linkedin_url"] = linkedin_url

    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(
                f"{_BASE}/crm/v3/objects/contacts",
                headers=hdrs,
                json={"properties": props},
            )
        if r.status_code in (200, 201):
            return str(r.json().get("id", ""))
        raise HubSpotClientError(f"create_contact {r.status_code}: {r.text[:200]}")
    except HubSpotClientError:
        raise
    except Exception as exc:
        raise HubSpotClientError(f"create_contact network error: {exc}") from exc


def create_deal(
    *,
    deal_name: str,
    pipeline_id: str = PIPELINE_F3E_RETAIL,
    stage_id: str = _F3E_STAGE_IDENTIFY,
    contact_id: str | None = None,
    owner_id: str | None = None,
) -> str:
    """Create a HubSpot deal and optionally associate a contact. Returns deal ID."""
    hdrs = _headers()
    props: dict[str, str] = {
        "dealname": deal_name,
        "pipeline": pipeline_id,
        "dealstage": stage_id,
    }
    if owner_id:
        props["hubspot_owner_id"] = owner_id

    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(
                f"{_BASE}/crm/v3/objects/deals",
                headers=hdrs,
                json={"properties": props},
            )
        if r.status_code not in (200, 201):
            raise HubSpotClientError(f"create_deal {r.status_code}: {r.text[:200]}")

        deal_id = str(r.json().get("id", ""))

        if contact_id and deal_id:
            try:
                with httpx.Client(timeout=_TIMEOUT) as c2:
                    c2.put(
                        f"{_BASE}/crm/v4/objects/contacts/{contact_id}/associations/deals/{deal_id}",
                        headers=hdrs,
                        json=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 4}],
                    )
            except Exception as exc:
                log.warning("HubSpot contact-deal association failed: %s", exc)

        return deal_id

    except HubSpotClientError:
        raise
    except Exception as exc:
        raise HubSpotClientError(f"create_deal network error: {exc}") from exc


def create_note(
    *,
    body: str,
    deal_id: str | None = None,
    contact_id: str | None = None,
) -> str:
    """Create a HubSpot note and associate with a deal and/or contact. Returns note ID."""
    import time as _time

    hdrs = _headers()
    ts_ms = str(int(_time.time() * 1000))
    props = {"hs_note_body": body[:65535], "hs_timestamp": ts_ms}

    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(
                f"{_BASE}/crm/v3/objects/notes",
                headers=hdrs,
                json={"properties": props},
            )
        if r.status_code not in (200, 201):
            raise HubSpotClientError(f"create_note {r.status_code}: {r.text[:200]}")

        note_id = str(r.json().get("id", ""))

        for obj_type, obj_id, assoc_type_id in [
            ("deals", deal_id, 214),
            ("contacts", contact_id, 202),
        ]:
            if obj_id and note_id:
                try:
                    with httpx.Client(timeout=_TIMEOUT) as c2:
                        c2.put(
                            f"{_BASE}/crm/v4/objects/notes/{note_id}/associations/{obj_type}/{obj_id}",
                            headers=hdrs,
                            json=[{"associationCategory": "HUBSPOT_DEFINED",
                                   "associationTypeId": assoc_type_id}],
                        )
                except Exception as exc:
                    log.warning("HubSpot note-%s association failed: %s", obj_type, exc)

        return note_id

    except HubSpotClientError:
        raise
    except Exception as exc:
        raise HubSpotClientError(f"create_note network error: {exc}") from exc
