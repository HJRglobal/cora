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

_DB_ID = "7820cd3689ae4596bd8f965f2bf96d5d"  # Contracts & Renewals Registry
_API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"
_TIMEOUT = 15.0
_RATE_SLEEP = 0.2
_RENEWAL_WINDOW_DAYS = 75

# Media Contacts — Press Pipeline (fndr_press_pipeline_summary)
_PRESS_DB_ID = "b139a18460f447f0ab761ba0570bd4e2"
# Published-feature targets that gate Wikipedia AfC submission (press-first strategy,
# decisions.md 2026-06-07): F3 Energy first (3), Lexington second (2).
_PRESS_TARGETS: dict[str, int] = {"F3E": 3, "Lexington": 2}
_PRESS_ENTITY_LABELS: dict[str, str] = {"F3E": "F3 Energy", "Lexington": "Lexington"}
# Display order for the Status breakdown header (matches the Notion select options).
_PRESS_STATUS_ORDER = [
    "Sourced",
    "To pitch",
    "Pitched",
    "Responded",
    "Published",
    "Passed",
]


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


def _query_db(
    filter_body: dict | None = None,
    start_cursor: str | None = None,
    db_id: str | None = None,
) -> dict:
    """Single page of results from a Notion DB (defaults to the Contracts DB)."""
    db = db_id or _DB_ID
    time.sleep(_RATE_SLEEP)
    body: dict = {"page_size": 100}
    if filter_body:
        body["filter"] = filter_body
    if start_cursor:
        body["start_cursor"] = start_cursor
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(f"{_API_BASE}/databases/{db}/query", headers=_headers(), json=body)
    if r.status_code == 401:
        raise NotionClientError("Notion 401 — API key invalid or no DB access")
    if r.status_code == 404:
        raise NotionClientError(f"Notion 404 — DB {db} not found or not connected to integration")
    if r.status_code == 429:
        time.sleep(float(r.headers.get("Retry-After", "2")))
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(f"{_API_BASE}/databases/{db}/query", headers=_headers(), json=body)
    if r.status_code not in (200, 201):
        raise NotionClientError(f"Notion {r.status_code}: {r.text[:200]}")
    return r.json()


def _paginate(filter_body: dict | None = None, db_id: str | None = None) -> list[dict]:
    """Return all pages matching filter_body across cursors (defaults to Contracts DB)."""
    results: list[dict] = []
    cursor: str | None = None
    while True:
        resp = _query_db(filter_body=filter_body, start_cursor=cursor, db_id=db_id)
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


def _rich_text(props: dict, name: str) -> str:
    items = (props.get(name) or {}).get("rich_text", [])
    return "".join(t.get("plain_text", "") for t in items).strip()


def _url(props: dict, name: str) -> str | None:
    return (props.get(name) or {}).get("url") or None


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


# ---------------------------------------------------------------------------
# Press pipeline summary (fndr_press_pipeline_summary)
# ---------------------------------------------------------------------------


def get_press_pipeline_summary_text() -> str:
    """Return a Slack mrkdwn summary of the Media Contacts -- Press Pipeline.

    Sections:
      - Header: total contacts + breakdown by Status.
      - Published progress per entity (F3 Energy target 3, Lexington target 2) --
        the headline metric gating Wikipedia AfC submission. "Both"-tagged
        features count toward both entities.
      - ACTIVE: rows with Status = Pitched or Responded.
      - TO PITCH: rows with Status = To pitch.

    Source-opaque (no DB/tool names). Deep links to rows where available.
    """
    try:
        pages = _paginate(db_id=_PRESS_DB_ID)
    except NotionClientError as exc:
        log.error("fndr_press_pipeline_summary: Notion error: %s", exc)
        return (
            "I don't have that right now. If this keeps happening, the Media Contacts "
            "press pipeline may need to be shared with my Notion integration."
        )

    rows: list[dict] = []
    for page in pages:
        props = page.get("properties", {})
        reporter = _title(props, "Reporter")
        if not reporter:
            continue
        rows.append(
            {
                "reporter": reporter,
                "outlet": _rich_text(props, "Outlet"),
                "angle": _select(props, "Angle"),
                "status": _select(props, "Status") or "(no status)",
                "entity": _select(props, "Entity"),
                "date_pitched": _date_start(props, "Date Pitched"),
                "coverage_link": _url(props, "Coverage Link"),
                "page_url": page.get("url", ""),
            }
        )

    if not rows:
        return "The press pipeline is empty -- no media contacts logged yet."

    # --- Header: total + status breakdown ---
    status_counts: dict[str, int] = {}
    for r in rows:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    ordered = [s for s in _PRESS_STATUS_ORDER if status_counts.get(s)]
    extra = sorted(s for s in status_counts if s not in _PRESS_STATUS_ORDER)
    breakdown = " · ".join(f"{s} {status_counts[s]}" for s in (ordered + extra))

    lines = [f"*Press Pipeline — {len(rows)} contacts:*", breakdown, ""]

    # --- Published progress per entity (headline metric) ---
    lines.append("*Published progress (Wikipedia AfC gate):*")
    for ent_key, target in _PRESS_TARGETS.items():
        label = _PRESS_ENTITY_LABELS.get(ent_key, ent_key)
        pub_rows = [
            r for r in rows
            if r["status"] == "Published" and r["entity"] in (ent_key, "Both")
        ]
        count = len(pub_rows)
        marker = "✅" if count >= target else "⏳"
        lines.append(f"{marker} *{label}:* {count}/{target} published")
        for r in pub_rows:
            label_txt = " — ".join(p for p in (r["reporter"], r["outlet"]) if p)
            link = r["coverage_link"] or r["page_url"]
            lines.append(f"   • <{link}|{label_txt}>" if link else f"   • {label_txt}")
    lines.append("")

    # --- ACTIVE (Pitched / Responded), oldest-pitched first to surface stale follow-ups ---
    active = [r for r in rows if r["status"] in ("Pitched", "Responded")]
    active.sort(key=lambda r: (r["date_pitched"] is None, r["date_pitched"] or ""))
    lines.append(f"*Active ({len(active)}):*")
    if active:
        for r in active:
            name = f"<{r['page_url']}|{r['reporter']}>" if r["page_url"] else f"*{r['reporter']}*"
            parts = [name]
            if r["outlet"]:
                parts.append(r["outlet"])
            if r["angle"]:
                parts.append(r["angle"])
            parts.append(f"[{r['status']}]")
            if r["date_pitched"]:
                parts.append(f"pitched {r['date_pitched']}")
            lines.append(f"• {' — '.join(parts)}")
    else:
        lines.append("_none_")
    lines.append("")

    # --- TO PITCH ---
    to_pitch = [r for r in rows if r["status"] == "To pitch"]
    lines.append(f"*To pitch ({len(to_pitch)}):*")
    if to_pitch:
        for r in to_pitch:
            name = f"<{r['page_url']}|{r['reporter']}>" if r["page_url"] else f"*{r['reporter']}*"
            parts = [name]
            if r["outlet"]:
                parts.append(r["outlet"])
            if r["angle"]:
                parts.append(r["angle"])
            lines.append(f"• {' — '.join(parts)}")
    else:
        lines.append("_none_")

    log.info(
        "fndr_press_pipeline_summary: %d contacts, %d active, %d to-pitch",
        len(rows), len(active), len(to_pitch),
    )
    return "\n".join(lines).rstrip()
