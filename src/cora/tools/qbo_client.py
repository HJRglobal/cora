"""QuickBooks Online Accounting REST client — read-only, per-entity.

Phase 2 #10 part 2 scope (Cora tool surface):
  - get_profit_loss(entity, start_date, end_date)
  - get_balance_sheet(entity, as_of_date)
  - get_ar_aging(entity)
  - get_ap_aging(entity)
  - get_recent_transactions(entity, days=30)

All five wrap QBO's Reports API and return both the raw JSON and a Slack-mrkdwn-
ready summary string with a deep link back to the QBO web UI for the report.

Authentication: tokens come from cora.connectors.qbo_oauth.get_valid_access_token,
which transparently refreshes if the cached token is near expiry.

Deep links: QBO web UI URLs are realm_id-scoped. See _QBO_REPORT_URLS for the
patterns. Verify against actual production URLs the first time you click through.

Channel-scoping: enforced upstream in tool_dispatch.py via TIER_1 channel-function
gating + the qbo-tokens.json being keyed by entity. A LEX-finance channel can
only see Lex entities' tokens; an F3E-sales channel sees nothing (financial
guardrail blocks the tool entirely).
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

import httpx

from ..connectors.qbo_oauth import (
    QboAuthError,
    get_valid_access_token,
)

log = logging.getLogger(__name__)


class QboClientError(Exception):
    """Raised on any QBO REST API failure."""


# QBO API base URLs. The OAuth tokens themselves are bound to a specific
# environment (production vs sandbox) at provisioning time, captured in
# qbo-tokens.json. The base URL is selected per token entry.
_API_BASE_PRODUCTION = "https://quickbooks.api.intuit.com"
_API_BASE_SANDBOX = "https://sandbox-quickbooks.api.intuit.com"

# QBO web UI deep-link templates. {realm_id} substituted at format time.
# These open the report in the QBO UI scoped to the right company. Verify after
# first successful smoke test — Intuit occasionally updates the URL shape.
_QBO_REPORT_URLS = {
    "profit_and_loss":  "https://qbo.intuit.com/app/profitandloss?reset=1",
    "balance_sheet":    "https://qbo.intuit.com/app/balancesheet?reset=1",
    "ar_aging":         "https://qbo.intuit.com/app/agedreceivables?reset=1",
    "ap_aging":         "https://qbo.intuit.com/app/agedpayables?reset=1",
    "transactions":     "https://qbo.intuit.com/app/transactions",
}

_DEFAULT_TIMEOUT = 30.0


def _api_base_for_entity(entity: str) -> str:
    """Pick prod vs sandbox base URL based on what's recorded in the token store."""
    # Local import to avoid circular deps at module load time.
    from ..connectors.qbo_oauth import _load_all_tokens  # noqa: PLC0415

    tokens = _load_all_tokens()
    env = (tokens.get(entity) or {}).get("environment") or "production"
    return _API_BASE_SANDBOX if env == "sandbox" else _API_BASE_PRODUCTION


def _request(entity: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Internal GET helper. Resolves token, sets Auth header, returns parsed JSON.

    `path` is appended to the company-scoped API base, e.g.
        /v3/company/{realm_id}/reports/ProfitAndLoss
    {realm_id} is substituted from the token entry.
    """
    try:
        access_token, realm_id = get_valid_access_token(entity)
    except QboAuthError as exc:
        raise QboClientError(f"QBO auth error for entity={entity}: {exc}") from exc

    url = _api_base_for_entity(entity) + path.format(realm_id=realm_id)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    log.info("QBO GET entity=%s path=%s params=%s", entity, path, params or {})
    try:
        resp = httpx.get(url, headers=headers, params=params or {}, timeout=_DEFAULT_TIMEOUT)
    except httpx.HTTPError as exc:
        raise QboClientError(f"HTTP error reaching QBO for entity={entity}: {exc}") from exc

    if resp.status_code == 401:
        # Token may have rotated under us; force-refresh once and retry. This shouldn't
        # happen often given get_valid_access_token's lead-time refresh, but covers the
        # narrow race where another process refreshed mid-request.
        from ..connectors.qbo_oauth import _refresh_access_token  # noqa: PLC0415
        log.warning("QBO returned 401 for entity=%s — forcing refresh and retrying", entity)
        _refresh_access_token(entity)
        access_token, realm_id = get_valid_access_token(entity)
        headers["Authorization"] = f"Bearer {access_token}"
        resp = httpx.get(url, headers=headers, params=params or {}, timeout=_DEFAULT_TIMEOUT)

    if resp.status_code != 200:
        raise QboClientError(
            f"QBO API error for entity={entity}: HTTP {resp.status_code} — {resp.text[:400]}"
        )

    try:
        return resp.json()
    except ValueError as exc:
        raise QboClientError(f"QBO returned non-JSON for entity={entity}: {exc}") from exc


def _realm_id(entity: str) -> str:
    from ..connectors.qbo_oauth import _get_entity_tokens  # noqa: PLC0415
    return _get_entity_tokens(entity)["realm_id"]


def _deep_link(report_key: str, realm_id: str) -> str:
    """Return a Slack-mrkdwn-ready hyperlink for a QBO report URL."""
    base = _QBO_REPORT_URLS.get(report_key, "https://qbo.intuit.com")
    # Append realm scoping — QBO supports `&companyId={realm_id}` on most report URLs.
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}companyId={realm_id}"


# ────────────────────────────────────────────────────────────────────────────
# Endpoints — read-only Reports API
# ────────────────────────────────────────────────────────────────────────────


def _default_date_range(days_back: int = 30) -> tuple[str, str]:
    today = datetime.date.today()
    start = today - datetime.timedelta(days=days_back)
    return start.isoformat(), today.isoformat()


def get_profit_loss(
    entity: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    """Fetch ProfitAndLoss report. Defaults to last 30 days if dates omitted."""
    if not start_date or not end_date:
        start_date, end_date = _default_date_range(30)
    return _request(
        entity,
        "/v3/company/{realm_id}/reports/ProfitAndLoss",
        params={"start_date": start_date, "end_date": end_date, "minorversion": "65"},
    )


def get_balance_sheet(entity: str, as_of_date: str | None = None) -> dict[str, Any]:
    """Fetch BalanceSheet report. Defaults to today if as_of_date omitted."""
    if not as_of_date:
        as_of_date = datetime.date.today().isoformat()
    return _request(
        entity,
        "/v3/company/{realm_id}/reports/BalanceSheet",
        params={"as_of_date": as_of_date, "minorversion": "65"},
    )


def get_ar_aging(entity: str) -> dict[str, Any]:
    """Fetch AgedReceivables (AR aging) summary."""
    return _request(
        entity,
        "/v3/company/{realm_id}/reports/AgedReceivables",
        params={"minorversion": "65"},
    )


def get_ap_aging(entity: str) -> dict[str, Any]:
    """Fetch AgedPayables (AP aging) summary."""
    return _request(
        entity,
        "/v3/company/{realm_id}/reports/AgedPayables",
        params={"minorversion": "65"},
    )


def get_recent_transactions(entity: str, days: int = 30) -> dict[str, Any]:
    """Query recent Invoice + Bill + Payment rows via the QBO query endpoint.

    Returns the union (separate top-level keys per type) so a formatter can render
    a 'recent activity' digest without three separate API calls.
    """
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    queries = {
        "invoices": f"SELECT * FROM Invoice WHERE MetaData.LastUpdatedTime > '{cutoff}' MAXRESULTS 25",
        "bills":    f"SELECT * FROM Bill WHERE MetaData.LastUpdatedTime > '{cutoff}' MAXRESULTS 25",
        "payments": f"SELECT * FROM Payment WHERE MetaData.LastUpdatedTime > '{cutoff}' MAXRESULTS 25",
    }
    out: dict[str, Any] = {}
    for key, query in queries.items():
        try:
            data = _request(
                entity,
                "/v3/company/{realm_id}/query",
                params={"query": query, "minorversion": "65"},
            )
            out[key] = data.get("QueryResponse", {})
        except QboClientError as exc:
            log.warning("Recent-transactions sub-query %s failed for entity=%s: %s", key, entity, exc)
            out[key] = {"error": str(exc)}
    return out


# ────────────────────────────────────────────────────────────────────────────
# LLM formatters — render report JSON as Slack-mrkdwn-ready text + deep link
# ────────────────────────────────────────────────────────────────────────────


def _extract_top_level_sections(report: dict[str, Any]) -> dict[str, str]:
    """Walk a QBO report structure for top-level section names + summary totals.

    QBO returns Reports as a nested tree of Rows. Top-level Summary rows carry
    the rolled-up totals — Income / Cost of Goods Sold / Net Income for P&L;
    Current Assets / Total Equity etc. for Balance Sheet; aging buckets for
    AR/AP. This extractor is structure-agnostic and works across all of them.
    Falls back to an empty dict if the shape shifts (Intuit occasionally
    reshapes report payloads).
    """
    out: dict[str, str] = {}
    rows = (report.get("Rows") or {}).get("Row") or []
    for row in rows:
        if row.get("type") != "Section":
            continue
        header = (row.get("Header") or {}).get("ColData", [{}])
        section_name = header[0].get("value", "") if header else ""
        summary = (row.get("Summary") or {}).get("ColData") or []
        if len(summary) >= 2 and section_name:
            out[section_name] = summary[-1].get("value", "")
    return out


def format_pnl_for_llm(
    report: dict[str, Any],
    entity: str,
    start_date: str,
    end_date: str,
) -> str:
    """Render a P&L into a few lines of Slack-mrkdwn for Claude to use in a reply."""
    try:
        realm_id = _realm_id(entity)
    except QboAuthError:
        realm_id = "unknown"
    link = _deep_link("profit_and_loss", realm_id)

    header = (report.get("Header") or {})
    report_name = header.get("ReportName", "Profit and Loss")
    period = f"{start_date} → {end_date}"

    totals = _extract_top_level_sections(report)
    if not totals:
        return (
            f"QBO {report_name} for {entity} ({period}) returned no summary rows. "
            f"Full report: <{link}|Open in QuickBooks>. Tell the user to open it directly."
        )

    lines = [f"QBO {report_name} for {entity} ({period}):"]
    for section, value in totals.items():
        lines.append(f"  • {section}: {value}")
    lines.append(f"Open in QBO: <{link}|{report_name} for {entity}>")
    return "\n".join(lines)


def format_balance_sheet_for_llm(report: dict[str, Any], entity: str, as_of_date: str) -> str:
    """Render Balance Sheet top-line numbers + deep link."""
    try:
        realm_id = _realm_id(entity)
    except QboAuthError:
        realm_id = "unknown"
    link = _deep_link("balance_sheet", realm_id)

    totals = _extract_top_level_sections(report)  # same extractor — sections live at top level too
    lines = [f"QBO Balance Sheet for {entity} (as of {as_of_date}):"]
    if totals:
        for section, value in totals.items():
            lines.append(f"  • {section}: {value}")
    else:
        lines.append("  (no summary rows in response — see full report)")
    lines.append(f"Open in QBO: <{link}|Balance Sheet for {entity}>")
    return "\n".join(lines)


def format_ar_aging_for_llm(report: dict[str, Any], entity: str) -> str:
    """Render AR aging buckets + deep link."""
    try:
        realm_id = _realm_id(entity)
    except QboAuthError:
        realm_id = "unknown"
    link = _deep_link("ar_aging", realm_id)
    totals = _extract_top_level_sections(report)
    lines = [f"QBO AR Aging for {entity}:"]
    if totals:
        for section, value in totals.items():
            lines.append(f"  • {section}: {value}")
    else:
        lines.append("  (no aging buckets in response — see full report)")
    lines.append(f"Open in QBO: <{link}|AR Aging for {entity}>")
    return "\n".join(lines)


def format_ap_aging_for_llm(report: dict[str, Any], entity: str) -> str:
    """Render AP aging buckets + deep link."""
    try:
        realm_id = _realm_id(entity)
    except QboAuthError:
        realm_id = "unknown"
    link = _deep_link("ap_aging", realm_id)
    totals = _extract_top_level_sections(report)
    lines = [f"QBO AP Aging for {entity}:"]
    if totals:
        for section, value in totals.items():
            lines.append(f"  • {section}: {value}")
    else:
        lines.append("  (no aging buckets in response — see full report)")
    lines.append(f"Open in QBO: <{link}|AP Aging for {entity}>")
    return "\n".join(lines)


def format_recent_transactions_for_llm(payload: dict[str, Any], entity: str, days: int) -> str:
    """Render a 'recent activity' digest with counts + open links."""
    try:
        realm_id = _realm_id(entity)
    except QboAuthError:
        realm_id = "unknown"
    link = _deep_link("transactions", realm_id)

    # QBO QueryResponse keys are the singular capitalized QBO entity names.
    _qbo_response_keys = {"invoices": "Invoice", "bills": "Bill", "payments": "Payment"}

    lines = [f"QBO recent activity for {entity} (last {days} days):"]
    for kind in ("invoices", "bills", "payments"):
        section = payload.get(kind) or {}
        if "error" in section:
            lines.append(f"  • {kind}: error fetching ({section['error'][:80]})")
            continue
        items = section.get(_qbo_response_keys[kind]) or []
        lines.append(f"  • {kind}: {len(items)} updated")
    lines.append(f"Open in QBO: <{link}|Transactions for {entity}>")
    return "\n".join(lines)
