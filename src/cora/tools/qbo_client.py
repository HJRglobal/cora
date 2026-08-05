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
        log.warning("QBO returned 401 for entity=%s - forcing refresh and retrying", entity)
        _refresh_access_token(entity)
        access_token, realm_id = get_valid_access_token(entity)
        headers["Authorization"] = f"Bearer {access_token}"
        resp = httpx.get(url, headers=headers, params=params or {}, timeout=_DEFAULT_TIMEOUT)

    if resp.status_code != 200:
        raise QboClientError(
            f"QBO API error for entity={entity}: HTTP {resp.status_code} - {resp.text[:400]}"
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


def parse_period(period: str | None) -> tuple[str, str]:
    """Resolve a user-supplied period string to (start_date, end_date) ISO strings.

    Accepts (case-insensitive):
      - "this_month"      first of current month through today
      - "last_month"      first through last day of previous month
      - "ytd"             Jan 1 of current year through today
      - "last_year"       Jan 1 through Dec 31 of previous year
      - "last_30_days"    today - 30 days through today
      - "last_90_days"    today - 90 days through today
      - "YYYY-MM-DD to YYYY-MM-DD"  explicit range
      - None / unrecognized  defaults to last_30_days

    Raises ValueError only if the explicit range parses to invalid dates.
    """
    today = datetime.date.today()
    norm = (period or "").strip().lower().replace("-", "_").replace(" ", "_")

    if not norm or norm == "last_30_days":
        return (today - datetime.timedelta(days=30)).isoformat(), today.isoformat()
    if norm == "last_90_days":
        return (today - datetime.timedelta(days=90)).isoformat(), today.isoformat()
    if norm == "this_month":
        return today.replace(day=1).isoformat(), today.isoformat()
    if norm == "last_month":
        first_this = today.replace(day=1)
        last_prev = first_this - datetime.timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return first_prev.isoformat(), last_prev.isoformat()
    if norm == "ytd":
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    if norm == "last_year":
        last_year = today.year - 1
        return f"{last_year}-01-01", f"{last_year}-12-31"

    # Explicit "YYYY-MM-DD to YYYY-MM-DD" form (normalize separator)
    raw = (period or "").lower().replace("_to_", " to ").replace(" to ", "|")
    parts = raw.split("|")
    if len(parts) == 2:
        try:
            start = datetime.date.fromisoformat(parts[0].strip())
            end = datetime.date.fromisoformat(parts[1].strip())
            return start.isoformat(), end.isoformat()
        except ValueError:
            pass

    # Unrecognized - fall back to last 30 days
    return (today - datetime.timedelta(days=30)).isoformat(), today.isoformat()


# QBO Class IDs for HJRP sub-properties (used to filter P&L by building).
# Values are QBO integer Class IDs (from SELECT * FROM Class), NOT class names.
# Class names are human-readable labels; the API requires the numeric Id field.
# Confirmed live against HJRP realm 123145677834422 on 2026-06-04.
_HJRP_CLASS_MAP: dict[str, str] = {
    "HJRP-1337": "3600000000001380786",   # North Hampton -- 1337 S Gilbert Rd (7005-105 1337 Bldg)
    "HJRP-1555": "3600000000001148584",   # South Hampton -- 1555 S Gilbert Rd (9400-703 LexBuilding Three)
    "HJRP-RR":   "568350",               # Rogers Ranch -- Payson property (1715 Payson Cabin in QBO)
    # NOTE: Rogers Ranch and Payson Cabin are the same property. Internal name = Rogers Ranch.
    # QBO class name = "1715 Payson Cabin". Always treat these as identical when answering questions.
}


# Per-entity accounting-basis policy for the CONVERSATIONAL P&L path (WS6).
#
# EMPTY BY DEFAULT, AND DELIBERATELY SO. The conversational tool labels whatever
# basis QBO actually rendered (read from the report header — see _report_basis),
# which is always TRUTHFUL. Listing an entity here FORCES that basis instead of
# the company default — use it to make a conversational P&L match the basis of the
# books Harrison files for that entity.
#
# Do NOT blanket-set "Accrual": LEX-LLC is genuinely cash-basis and LBHS differs
# (42-CFR-Part-2), so a global default would produce confidently-wrong figures.
# Harrison/Justin populate this per entity once each entity's filed basis is
# confirmed, e.g.:
#     "F3E": "Accrual",
#     "LEX-LLC": "Cash",
# (The OSN per-store metrics digest pins Accrual independently in
# run_osn_metrics_digest.py; that path is unchanged.)
_ENTITY_PNL_BASIS: dict[str, str] = {}


def entity_pnl_basis(entity: str) -> str | None:
    """Return the configured P&L accounting-basis override for an entity, or None.

    None means "use the company's own QBO default" (the conversational reply then
    labels whatever basis QBO actually rendered). A value ("Accrual"/"Cash") forces
    that basis. Case-insensitive entity lookup.
    """
    if not entity:
        return None
    return _ENTITY_PNL_BASIS.get(entity.upper())


def get_profit_loss(
    entity: str,
    start_date: str | None = None,
    end_date: str | None = None,
    class_ref: str | None = None,
    accounting_method: str | None = None,
) -> dict[str, Any]:
    """Fetch ProfitAndLoss report. Defaults to last 30 days if dates omitted.

    For HJRP sub-properties (HJRP-1337, HJRP-1555), pass the entity code and
    the class_ref will be auto-resolved from _HJRP_CLASS_MAP. Alternatively
    pass class_ref explicitly to filter by a specific QBO class code.

    accounting_method: "Accrual" or "Cash". When omitted (default), QBO renders
    the report in each COMPANY's own default report basis -- which differs per
    realm, so figures from different companies are not comparable. Pass an
    explicit basis when comparing across realms (e.g. the OSN per-store digest
    pins "Accrual" so all 4 stores are on the same basis and the label is true).
    """
    if not start_date or not end_date:
        start_date, end_date = _default_date_range(30)
    params: dict = {"start_date": start_date, "end_date": end_date, "minorversion": "65"}

    if accounting_method:
        params["accounting_method"] = accounting_method

    # Auto-resolve class for HJRP sub-property entities
    resolved_class = class_ref or _HJRP_CLASS_MAP.get(entity)
    if resolved_class:
        params["class"] = resolved_class

    # HJRP sub-properties use the parent HJRP token
    token_entity = "HJRP" if entity in _HJRP_CLASS_MAP else entity

    return _request(
        token_entity,
        "/v3/company/{realm_id}/reports/ProfitAndLoss",
        params=params,
    )


def get_balance_sheet(entity: str, as_of_date: str | None = None) -> dict[str, Any]:
    """Fetch BalanceSheet report. Defaults to today if as_of_date omitted."""
    if not as_of_date:
        as_of_date = datetime.date.today().isoformat()
    token_entity = "HJRP" if entity in _HJRP_CLASS_MAP else entity
    return _request(
        token_entity,
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


def _report_basis(report: dict[str, Any]) -> str | None:
    """Return the accounting basis QBO actually used to render a report.

    Reads ``Header.ReportBasis`` ("Accrual" / "Cash"). Returns None when the field
    is absent so a formatter can OMIT the label rather than fabricate a basis. This
    is the truthful signal of what the figures represent regardless of whether an
    accounting_method override was passed.
    """
    basis = ((report or {}).get("Header") or {}).get("ReportBasis")
    if isinstance(basis, str) and basis.strip():
        return basis.strip()
    return None


def _parse_money(raw: str | None) -> float | None:
    """Parse a QBO summary value string into a float USD, or None if unparseable.

    Handles plain decimals ("12345.67"), thousands separators ("12,345.67"), a
    leading "$", and accounting-negative parentheses ("(1,234.56)" -> -1234.56).
    """
    if not raw:
        return None
    t = raw.strip().replace(",", "").replace("$", "").strip()
    negative = t.startswith("(") and t.endswith(")")
    t = t.strip("()").strip()
    if not t:
        return None
    try:
        value = float(t)
    except ValueError:
        return None
    return -value if negative else value


def extract_pnl_revenue(report: dict[str, Any]) -> float | None:
    """Return the top-line Income (revenue) total from a P&L report as float USD.

    This is source-opaque accrual revenue from the books -- NOT a register /
    payment gross total, so it will not match a prior point-of-sale figure 1:1.
    Returns None when the report has no recognizable Income section (so a caller
    can skip that entity rather than mis-report $0).
    """
    totals = _extract_top_level_sections(report)
    if not totals:
        return None
    # QBO labels the top revenue section "Income"; some report shapes use
    # "Total Income". Match those exactly (case-insensitive) first.
    for name, value in totals.items():
        if name.strip().lower() in ("income", "total income"):
            return _parse_money(value)
    # Fallback: an income-ish section that is neither "Other Income" nor the
    # "Net Income" bottom line. Exclude "net income" as a phrase (not any "net"
    # substring) so a legit "Net Sales Income"-style revenue line still matches.
    for name, value in totals.items():
        nl = name.strip().lower()
        if "income" in nl and "other" not in nl and not nl.startswith("net income"):
            return _parse_money(value)
    return None


def format_pnl_for_llm(
    report: dict[str, Any],
    entity: str,
    start_date: str,
    end_date: str,
) -> str:
    """Render a P&L into a few lines of Slack-mrkdwn for Claude to use in a reply.

    Source-opaque (B2): names the report type, never the system. QBO tools are
    VERBATIM_TABLE_TOOLS so this output bypasses reply_formatter's source-opacity
    lint, and the egress boundary preserves sanctioned <url|label> links -- so any
    'QBO'/'Open in QBO'/intuit deep link emitted here would reach Slack verbatim.
    Strip it at source instead.
    """
    period = f"{start_date} to {end_date}"
    basis = _report_basis(report)
    # Truthful basis label (WS6): figures on a different basis are not comparable
    # across entities, and a cash-basis figure legitimately disagrees with an
    # accrual filed report — labelling makes that explicable instead of silent.
    basis_tag = f" [{basis} basis]" if basis else ""

    totals = _extract_top_level_sections(report)
    if not totals:
        return (
            f"Profit and Loss for {entity} ({period}){basis_tag} returned no summary rows. "
            f"Ask finance for the detailed report."
        )

    lines = [f"Profit and Loss for {entity} ({period}){basis_tag}:"]
    for section, value in totals.items():
        lines.append(f"  • {section}: {value}")
    return "\n".join(lines)


def format_balance_sheet_for_llm(report: dict[str, Any], entity: str, as_of_date: str) -> str:
    """Render Balance Sheet top-line numbers. Source-opaque (B2 -- see format_pnl)."""
    totals = _extract_top_level_sections(report)  # same extractor — sections live at top level too
    lines = [f"Balance Sheet for {entity} (as of {as_of_date}):"]
    if totals:
        for section, value in totals.items():
            lines.append(f"  • {section}: {value}")
    else:
        lines.append("  (no summary rows in response)")
    return "\n".join(lines)


def format_ar_aging_for_llm(report: dict[str, Any], entity: str) -> str:
    """Render AR aging buckets. Source-opaque (B2 -- see format_pnl)."""
    totals = _extract_top_level_sections(report)
    lines = [f"AR Aging for {entity}:"]
    if totals:
        for section, value in totals.items():
            lines.append(f"  • {section}: {value}")
    else:
        lines.append("  (no aging buckets in response)")
    return "\n".join(lines)


def format_ap_aging_for_llm(report: dict[str, Any], entity: str) -> str:
    """Render AP aging buckets. Source-opaque (B2 -- see format_pnl)."""
    totals = _extract_top_level_sections(report)
    lines = [f"AP Aging for {entity}:"]
    if totals:
        for section, value in totals.items():
            lines.append(f"  • {section}: {value}")
    else:
        lines.append("  (no aging buckets in response)")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# Account-entity reads (A5 S1) — live per-account balances + books freshness
#
# These use the QUERY API, a different surface from the Reports API the rest of
# this module reads. That distinction is load-bearing and is NOT a rounding
# detail — see the CC_SIGN and REGISTER_VS_REPORT notes below, both verified
# live across all 11 realms on 2026-08-04.
# ────────────────────────────────────────────────────────────────────────────

# Verified live 2026-08-04: `AccountType in ('Bank','Credit Card')` parses;
# the chained-OR form `AccountType = 'Bank' or AccountType = 'Credit Card'`
# is REJECTED with HTTP 400 "Encountered <OR>". QBO's query language has no OR.
_ACCOUNT_TYPES = ("Bank", "Credit Card")

# `Active = true` is load-bearing, not hygiene: an unfiltered Account query
# returns closed/inactive accounts too, and any residual nonzero balance on one
# would silently inflate every total built from this list.
_ACCOUNT_QUERY = (
    "select Id, Name, AccountType, AccountSubType, CurrentBalance, CurrencyRef "
    "from Account where AccountType in ('Bank','Credit Card') and Active = true"
)

_ACCOUNT_PAGE_SIZE = 200

# Transaction types whose posting advances the BANK side of the books. Verified
# available live on every provisioned realm 2026-08-04.
#
# BillPayment is not optional: it is the standard pay-bills workflow, and a realm
# that pays bills that way (HJRG, HJRP, LEX, all four OSN store realms) would read
# as permanently stale without it.
_BANK_SIDE_TXN_TYPES = ("Purchase", "Deposit", "Transfer", "BillPayment")

# Payments are included only when they land DIRECTLY in a bank account. Verified
# live on F3E 2026-08-04: payments deposit either to a Bank account (id 9,
# "Tradition F3 8950") or to Undeposited Funds (id 215, an Other Current Asset) —
# the latter has NOT touched the bank yet, so counting it would report false
# freshness. `DepositToAccountRef` is NOT a queryable property (HTTP 400
# "property 'DepositToAccountRef' is not queryable"), so the filter is applied
# client-side over the newest few payments.
_PAYMENT_SCAN_LIMIT = 25


def _query(entity: str, query: str) -> dict[str, Any]:
    """Run one QBO query and return the QueryResponse object."""
    data = _request(
        entity,
        "/v3/company/{realm_id}/query",
        params={"query": query, "minorversion": "65"},
    )
    return data.get("QueryResponse") or {}


def query_accounts(entity: str) -> list[dict[str, Any]]:
    """Return this realm's ACTIVE Bank + Credit Card accounts, fully paginated.

    Each item: ``{"id", "name", "type", "subtype", "balance", "currency"}``.
    ``balance`` is ``Account.CurrentBalance`` — the account REGISTER balance —
    and is None when QBO omits or returns an unparseable value (never coerced
    to 0.0, which would read as a real zero balance).

    Paginated deliberately: a silently truncated account list understates every
    total built from it, which is the D-117 "looks clean, wasn't complete" class.
    """
    out: list[dict[str, Any]] = []
    start = 1
    while True:
        page = _query(
            entity,
            f"{_ACCOUNT_QUERY} STARTPOSITION {start} MAXRESULTS {_ACCOUNT_PAGE_SIZE}",
        )
        rows = page.get("Account") or []
        for row in rows:
            raw_balance = row.get("CurrentBalance")
            out.append({
                "id": str(row.get("Id") or ""),
                "name": str(row.get("Name") or ""),
                "type": str(row.get("AccountType") or ""),
                "subtype": str(row.get("AccountSubType") or ""),
                "balance": _coerce_balance(raw_balance),
                "currency": ((row.get("CurrencyRef") or {}).get("value") or ""),
            })
        if len(rows) < _ACCOUNT_PAGE_SIZE:
            break
        start += _ACCOUNT_PAGE_SIZE
    return out


def _coerce_balance(raw: Any) -> float | None:
    """QBO returns CurrentBalance as a JSON number, but be defensive: an absent or
    unparseable value must stay None so callers can render UNKNOWN, never 0."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    return _parse_money(str(raw))


def summarize_accounts(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    """Split accounts into bank/credit-card totals plus cash-net-of-cards.

    CC_SIGN (verified live across all 11 realms, 2026-08-04 — the named build
    gate from the design):
        Query-API ``Account.CurrentBalance`` reports a credit-card LIABILITY as a
        NEGATIVE number, while the BalanceSheet report's Credit Cards section
        reports the same liability POSITIVE. The cleanest proof was OSNVV, where
        the two surfaces returned the identical magnitude with opposite signs:
        query -3,945.64 vs report +3,945.64 (F3E -1,300.54 / +627.78 and
        HJRG -3,463.60 / +15,140.10 agree in direction).

        So cash net of cards is ``bank_total + cc_total`` here — adding a negative
        SUBTRACTS the card debt. Using the report-side convention's
        ``bank_total - cc_total`` on these values would ADD card debt to cash,
        overstating available cash by twice the balance.

    Totals skip accounts whose balance is None, and ``*_unknown`` counts say how
    many were skipped so a caller can refuse to present a total as complete.
    """
    bank = [a for a in accounts if a.get("type") == "Bank"]
    cards = [a for a in accounts if a.get("type") == "Credit Card"]

    def _total(rows: list[dict[str, Any]]) -> tuple[float, int]:
        known = [r["balance"] for r in rows if r.get("balance") is not None]
        return (round(sum(known), 2), len(rows) - len(known))

    bank_total, bank_unknown = _total(bank)
    cc_total, cc_unknown = _total(cards)

    return {
        "bank_count": len(bank),
        "cc_count": len(cards),
        "bank_total": bank_total,
        "cc_total": cc_total,
        # See CC_SIGN above: '+' is correct because cc_total is already negative.
        "cash_net_of_cards": round(bank_total + cc_total, 2),
        "bank_unknown": bank_unknown,
        "cc_unknown": cc_unknown,
        "balances_complete": (bank_unknown == 0 and cc_unknown == 0),
    }


def _row_account_ids(row: dict[str, Any]) -> set[str]:
    """Every account id a transaction row references, across the shapes QBO uses.

    Purchase carries `AccountRef` (the paying account); Transfer carries
    `FromAccountRef`/`ToAccountRef`; BillPayment nests the account under
    `CheckPayment.BankAccountRef` or `CreditCardPayment.CCAccountRef`; Payment
    uses `DepositToAccountRef`. Returns an EMPTY set when a row exposes none of
    them, which the caller treats as "cannot tell" -- not as "not a bank txn".
    """
    ids: set[str] = set()
    for key in ("AccountRef", "FromAccountRef", "ToAccountRef", "DepositToAccountRef"):
        value = (row.get(key) or {}).get("value") if isinstance(row.get(key), dict) else None
        if value:
            ids.add(str(value))
    for parent, child in (("CheckPayment", "BankAccountRef"),
                          ("CreditCardPayment", "CCAccountRef")):
        nested = row.get(parent)
        if isinstance(nested, dict):
            value = (nested.get(child) or {}).get("value") if isinstance(
                nested.get(child), dict) else None
            if value:
                ids.add(str(value))
    return ids


def _newest_bank_side(
    entity: str, txn_type: str, bank_account_ids: set[str], today: str,
) -> tuple[str | None, bool]:
    """(newest date that actually touched a BANK account, filtered?).

    WHY THIS FILTERS. A QBO `Purchase` with PaymentType=CreditCard never touches a
    bank account, and the type was previously counted unfiltered -- verified live
    2026-08-05 that LEX's newest Purchase sat on `Divvy Card Main` and 40 of its
    newest 60 Purchases were card-side. On a card-heavy realm that made the
    "newest posted bank-side txn" date wrong and, worse, meant the staleness flag
    could never fire: daily card spend kept the date fresh forever while the bank
    feed sat dead. Same exposure on Transfer and BillPayment.

    FAIL-SAFE: if no row in the window exposes ANY account reference (an unexpected
    schema, or a field QBO stops returning), fall back to the unfiltered newest
    date and report filtered=False, so the caller can label it. Losing the signal
    entirely would be a worse failure than an imprecise one.

    FUTURE-DATED rows are excluded from the max: QBO happily holds postdated
    entries, and letting one win made a realm render "0d ago" for a date months
    ahead, suppressing the staleness flag for as long as the date stayed forward.
    """
    page = _query(
        entity,
        f"select * from {txn_type} orderby TxnDate desc MAXRESULTS {_TXN_SCAN_LIMIT}",
    )
    rows = page.get(txn_type) or []
    posted = [r for r in rows if str(r.get("TxnDate") or "") <= today]
    if not posted:
        return None, True

    if not bank_account_ids:
        return max(str(r.get("TxnDate")) for r in posted), False

    matched = [r for r in posted if _row_account_ids(r) & bank_account_ids]
    if matched:
        return max(str(r.get("TxnDate")) for r in matched), True
    # Distinguish "scanned rows, none were bank-side" from "rows expose no account
    # reference at all" -- only the latter warrants the unfiltered fallback.
    if any(_row_account_ids(r) for r in posted):
        return None, True
    return max(str(r.get("TxnDate")) for r in posted), False


#: Rows scanned per transaction type when filtering to bank-side activity.
_TXN_SCAN_LIMIT = 50


def newest_bank_side_txn_date(
    entity: str,
    bank_account_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Newest posted BANK-SIDE transaction date for a realm.

    Returns ``{"date", "per_type", "types_covered", "types_expected", "errors"}``.

    This measures *what has posted*, not *whether the books are current* — QBO's
    public API exposes neither bank-feed connection status nor pending-review feed
    items. Close work done purely as journal entries does not advance this date,
    so consumers must render it as "newest posted bank-side txn", never as
    "books are stale", and flag only past a generous threshold.

    Fail-soft per type: one type erroring degrades coverage (reported through
    types_covered/types_expected) rather than losing the whole answer.
    """
    per_type: dict[str, str | None] = {}
    errors: dict[str, str] = {}
    unfiltered: list[str] = []
    ids = bank_account_ids or set()
    today = datetime.date.today().isoformat()

    scan_types = list(_BANK_SIDE_TXN_TYPES) + (["Payment"] if ids else [])
    for txn_type in scan_types:
        try:
            newest, filtered = _newest_bank_side(entity, txn_type, ids, today)
            per_type[txn_type] = newest
            if not filtered:
                unfiltered.append(txn_type)
        # Deliberately broad: QboAuthError is a SIBLING of QboClientError, not a
        # subclass, and _request's 401-retry path re-refreshes outside any guard --
        # so an auth failure (including a token-lock timeout) would otherwise
        # escape this per-type fail-soft and discard the balances we already read.
        except Exception as exc:  # noqa: BLE001
            log.warning("newest_bank_side_txn_date: %s failed for %s: %s", txn_type, entity, exc)
            errors[txn_type] = str(exc)[:200]
            per_type[txn_type] = None

    dates = [d for d in per_type.values() if d]
    expected = len(scan_types)

    # EVERY type returning zero rows, with no error raised, is far more likely an
    # API condition than a realm that has genuinely never transacted -- QBO was
    # observed 2026-08-05 returning an EMPTY QueryResponse (not an HTTP error) for
    # every transaction type while Account queries kept working. Reporting that as
    # clean full coverage would be a silent blind spot exactly where this section
    # is supposed to be watching.
    all_empty = not dates and not errors and expected > 0
    return {
        "date": max(dates) if dates else None,   # ISO YYYY-MM-DD sorts lexically
        "per_type": per_type,
        "types_covered": 0 if all_empty else expected - len(errors),
        "types_expected": expected,
        "errors": (
            {**errors, "_all_types_empty": "every txn type returned zero rows"}
            if all_empty else errors
        ),
        # Types whose date could NOT be narrowed to bank-side activity, so the date
        # may reflect card-side spend. Consumers must label it.
        "unfiltered_types": sorted(unfiltered),
    }


def format_recent_transactions_for_llm(payload: dict[str, Any], entity: str, days: int) -> str:
    """Render a 'recent activity' digest with counts. Source-opaque (B2 -- see format_pnl)."""
    # QueryResponse keys are the singular capitalized accounting entity names.
    _qbo_response_keys = {"invoices": "Invoice", "bills": "Bill", "payments": "Payment"}

    lines = [f"Recent activity for {entity} (last {days} days):"]
    for kind in ("invoices", "bills", "payments"):
        section = payload.get(kind) or {}
        if "error" in section:
            lines.append(f"  • {kind}: error fetching ({section['error'][:80]})")
            continue
        items = section.get(_qbo_response_keys[kind]) or []
        lines.append(f"  • {kind}: {len(items)} updated")
    return "\n".join(lines)
