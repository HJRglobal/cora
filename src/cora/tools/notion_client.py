"""Notion client — on-demand tool responses from the Contracts & Renewals Registry.

Distinct from notion_connector.py (which handles KB ingestion).
This module provides formatted Slack mrkdwn for real-time tool calls.

Auth: NOTION_API_KEY environment variable.
DB:   Contracts & Renewals Registry — 7820cd3689ae4596bd8f965f2bf96d5d
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta

import httpx

log = logging.getLogger(__name__)

_DB_ID = "7820cd3689ae4596bd8f965f2bf96d5d"
_API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"
_TIMEOUT = 15.0
_RATE_SLEEP = 0.2
_RENEWAL_WINDOW_DAYS = 75


class NotionClientError(Exception):
    pass


def _api_key() -> str:
    val = os.environ.get("NOTION_API_KEY", "")
    if not val:
        raise NotionClientError("NOTION_API_KEY not set — Notion tool disabled")
    return val


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _query_db(filter_body: dict | None = None, start_cursor: str | None = None) -> dict:
    """Single page of results from the Contracts DB."""
    time.sleep(_RATE_SLEEP)
    body: dict = {"page_size": 100}
    if filter_body:
        body["filter"] = filter_body
    if start_cursor:
        body["start_cursor"] = start_cursor
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(f"{_API_BASE}/databases/{_DB_ID}/query", headers=_headers(), json=body)
    if r.status_code == 401:
        raise NotionClientError("Notion 401 — API key invalid or no DB access")
    if r.status_code == 404:
        raise NotionClientError(f"Notion 404 — DB {_DB_ID} not found or not connected to integration")
    if r.status_code == 429:
        time.sleep(float(r.headers.get("Retry-After", "2")))
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(f"{_API_BASE}/databases/{_DB_ID}/query", headers=_headers(), json=body)
    if r.status_code not in (200, 201):
        raise NotionClientError(f"Notion {r.status_code}: {r.text[:200]}")
    return r.json()


def _paginate(filter_body: dict | None = None) -> list[dict]:
    """Return all pages matching filter_body across cursors."""
    results: list[dict] = []
    cursor: str | None = None
    while True:
        resp = _query_db(filter_body=filter_body, start_cursor=cursor)
        results.extend(resp.get("results", []))
        if not resp.get("has_more") or not resp.get("next_cursor"):
            break
        cursor = resp["next_cursor"]
    return results


# ---------------------------------------------------------------------------
# Property extraction helpers (minimal — only what the tool needs)
# ---------------------------------------------------------------------------


def _title(props: dict, name: str) -> str:
    items = (props.get(name) or {}).get("title", [])
    return "".join(t.get("plain_text", "") for t in items).strip()


def _select(props: dict, name: str) -> str | None:
    sel = (props.get(name) or {}).get("select")
    return sel.get("name") if sel else None


def _date_start(props: dict, name: str) -> str | None:
    d = (props.get(name) or {}).get("date")
    return d.get("start") if d else None


# ---------------------------------------------------------------------------
# Dashboard formatter
# ---------------------------------------------------------------------------


def get_contracts_dashboard_text() -> str:
    """Return Slack mrkdwn dashboard: renewals expiring ≤75d + all Escalate items.

    Emoji markers:
      🚨 = Escalate flag OR already expired (Term End in the past)
      🔴 = expiring ≤30d
      🟡 = expiring 31-75d
    """
    today = date.today()
    cutoff_iso = (today + timedelta(days=_RENEWAL_WINDOW_DAYS)).isoformat()

    # Notion OR filter: Term End before cutoff OR Risk Flag = Escalate
    # Notion date filters naturally skip null dates, so Escalate-only rows
    # (no Term End) come through solely via the second condition.
    filter_body = {
        "or": [
            {"property": "Term End", "date": {"before": cutoff_iso}},
            {"property": "Risk Flag", "select": {"equals": "Escalate"}},
        ]
    }

    try:
        pages = _paginate(filter_body=filter_body)
    except NotionClientError as exc:
        log.error("fndr_contracts_dashboard: Notion error: %s", exc)
        return "I don't have that right now."

    if not pages:
        return (
            f"No contracts expiring within {_RENEWAL_WINDOW_DAYS} days "
            "and no Escalate-flag items."
        )

    # Build normalised row dicts
    rows: list[dict] = []
    for page in pages:
        props = page.get("properties", {})
        title = _title(props, "Title")
        if not title:
            continue

        risk_flag = _select(props, "Risk Flag")
        term_end_str = _date_start(props, "Term End")
        status = _select(props, "Status") or ""
        page_url = page.get("url", "")

        days_remaining: int | None = None
        if term_end_str:
            try:
                days_remaining = (
                    datetime.strptime(term_end_str, "%Y-%m-%d").date() - today
                ).days
            except ValueError:
                pass

        in_window = days_remaining is not None and days_remaining <= _RENEWAL_WINDOW_DAYS
        is_escalate = risk_flag == "Escalate"

        if not in_window and not is_escalate:
            continue  # shouldn't happen with Notion filter, but guard anyway

        rows.append(
            {
                "title": title,
                "risk_flag": risk_flag,
                "term_end_str": term_end_str,
                "days_remaining": days_remaining,
                "status": status,
                "page_url": page_url,
                "in_window": in_window,
                "is_escalate": is_escalate,
            }
        )

    if not rows:
        return (
            f"No contracts expiring within {_RENEWAL_WINDOW_DAYS} days "
            "and no Escalate-flag items."
        )

    # Sort: expired first, then ascending days_remaining, then no-date last
    def _sort_key(r: dict) -> tuple:
        dr = r["days_remaining"]
        if dr is None:
            return (2, 0)    # no date: last
        if dr < 0:
            return (0, dr)   # already expired: first (most negative = oldest)
        return (1, dr)       # upcoming: ascending

    rows.sort(key=_sort_key)

    def _marker(r: dict) -> str:
        dr = r["days_remaining"]
        if r["is_escalate"] or (dr is not None and dr <= 0):
            return "🚨"
        if dr is not None and dr <= 30:
            return "🔴"
        return "🟡"

    def _fmt_row(r: dict) -> str:
        marker = _marker(r)
        label = r["title"]
        link = f"<{r['page_url']}|{label}>" if r["page_url"] else f"*{label}*"

        parts: list[str] = []
        if r["is_escalate"]:
            parts.append("Escalate")
        if r["term_end_str"]:
            dr = r["days_remaining"]
            if dr is None:
                parts.append(f"expires {r['term_end_str']}")
            elif dr < 0:
                parts.append(
                    f"EXPIRED {r['term_end_str']} ({abs(dr)}d ago)"
                )
            elif dr == 0:
                parts.append(f"expires TODAY ({r['term_end_str']})")
            else:
                parts.append(f"expires {r['term_end_str']} ({dr}d)")
        else:
            parts.append("no term end set")
        if r["status"] and r["status"] != "Active":
            parts.append(r["status"].lower())

        return f"{marker} {link} · {' · '.join(parts)}"

    # Header stats
    expired_count = sum(
        1 for r in rows
        if r["days_remaining"] is not None and r["days_remaining"] < 0
    )
    expiring_count = sum(
        1 for r in rows
        if r["in_window"] and r["days_remaining"] is not None and r["days_remaining"] >= 0
    )
    escalate_count = sum(1 for r in rows if r["is_escalate"])

    header_parts: list[str] = []
    if expired_count:
        header_parts.append(f"{expired_count} expired")
    if expiring_count:
        header_parts.append(f"{expiring_count} expiring ≤{_RENEWAL_WINDOW_DAYS}d")
    if escalate_count:
        header_parts.append(f"{escalate_count} Escalate")

    header = f"*Contracts — {', '.join(header_parts)}:*"

    lines = [header, ""]
    for r in rows:
        lines.append(_fmt_row(r))

    log.info("fndr_contracts_dashboard: %d rows returned", len(rows))
    return "\n".join(lines)
