"""Notion connector — syncs Contracts & Renewals Registry to the Cora knowledge base.

What we ingest:
    - Every entry in the Contracts & Renewals Registry Notion DB
      (DB ID: 7820cd3689ae4596bd8f965f2bf96d5d)
    - Each row becomes one Document: rich text block with all contract fields
    - Watermark: Notion page last_edited_time (server-side filter supported)

Entity mapping:
    Notion Entity field values → Cora KB entity codes.
    LEX sub-entities (LEX-LLC, LEX-LBHS, LEX-LLA, LEX-LTS) resolve to entity=LEX
    with sub_entity populated.
    "Personal" resolves to FNDR (personal finances fold into founder scope).

Auth:
    NOTION_API_KEY environment variable — integration token with read access
    to the Contracts & Renewals Registry database.

Notion API version: 2022-06-28
"""

import logging
import os
import time
from collections.abc import Iterator
from datetime import datetime, timezone

import httpx

from cora.knowledge_base.store import Document

log = logging.getLogger(__name__)

_DB_ID = "7820cd3689ae4596bd8f965f2bf96d5d"
_API_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"
_TIMEOUT = 20.0
_RATE_SLEEP = 0.2  # Notion integration: 3 req/s average; 200ms is safe

# Notion Entity select value → KB entity code.
# LEX sub-entities get entity=LEX + sub_entity=<value>.
# "Personal" folds into FNDR (founder-level financial scope).
_ENTITY_MAP: dict[str, str] = {
    "HJRG": "HJRG",
    "FNDR": "FNDR",
    "Personal": "FNDR",
    "F3E": "F3E",
    "F3C": "F3C",
    "UFL": "UFL",
    "OSN": "OSN",
    "BDM": "BDM",
    "HJRP": "HJRP",
    "HJRPROD": "HJRPROD",
    "LEX": "LEX",
    "LEX-LLC": "LEX",
    "LEX-LLA": "LEX",
    "LEX-LBHS": "LEX",
    "LEX-LTS": "LEX",
}

# Entity values that also carry a sub_entity tag
_SUB_ENTITY_MAP: dict[str, str] = {
    "LEX-LLC": "LEX-LLC",
    "LEX-LLA": "LEX-LLA",
    "LEX-LBHS": "LEX-LBHS",
    "LEX-LTS": "LEX-LTS",
}


class NotionConnectorError(Exception):
    pass


def _api_key() -> str:
    val = os.environ.get("NOTION_API_KEY", "")
    if not val:
        raise NotionConnectorError("NOTION_API_KEY not set — Notion connector disabled")
    return val


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _post(path: str, body: dict) -> dict:
    """POST to Notion API with auth + rate sleep."""
    time.sleep(_RATE_SLEEP)
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(f"{_API_BASE}{path}", headers=_headers(), json=body)
    if r.status_code == 401:
        raise NotionConnectorError("Notion 401 — API key invalid or missing database access")
    if r.status_code == 404:
        raise NotionConnectorError(f"Notion 404 — database not found: {_DB_ID}")
    if r.status_code == 429:
        retry_after = float(r.headers.get("Retry-After", "2"))
        log.warning("Notion 429 rate-limited; sleeping %.1fs", retry_after)
        time.sleep(retry_after)
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(f"{_API_BASE}{path}", headers=_headers(), json=body)
    if r.status_code >= 500:
        raise NotionConnectorError(f"Notion {r.status_code} upstream: {r.text[:200]}")
    if r.status_code not in (200, 201):
        raise NotionConnectorError(f"Notion {r.status_code}: {r.text[:200]}")
    return r.json()


def _query_db(filter_body: dict | None = None, start_cursor: str | None = None) -> dict:
    """Execute one page of a Notion database query."""
    body: dict = {"page_size": 100}
    if filter_body:
        body["filter"] = filter_body
    if start_cursor:
        body["start_cursor"] = start_cursor
    return _post(f"/databases/{_DB_ID}/query", body)


def _paginate_db(filter_body: dict | None = None) -> Iterator[dict]:
    """Yield all pages from the Contracts & Renewals Registry."""
    cursor: str | None = None
    while True:
        response = _query_db(filter_body=filter_body, start_cursor=cursor)
        for page in response.get("results", []):
            yield page
        has_more = response.get("has_more", False)
        cursor = response.get("next_cursor")
        if not has_more or not cursor:
            break


# ---------------------------------------------------------------------------
# Property extraction helpers
# ---------------------------------------------------------------------------


def _get_title(props: dict, name: str) -> str:
    """Extract plain text from a title property."""
    items = (props.get(name) or {}).get("title", [])
    return "".join(t.get("plain_text", "") for t in items).strip()


def _get_rich_text(props: dict, name: str) -> str:
    """Extract plain text from a rich_text property."""
    items = (props.get(name) or {}).get("rich_text", [])
    return "".join(t.get("plain_text", "") for t in items).strip()


def _get_select(props: dict, name: str) -> str | None:
    """Extract the selected option name from a select property."""
    sel = (props.get(name) or {}).get("select")
    return sel.get("name") if sel else None


def _get_checkbox(props: dict, name: str) -> bool:
    """Extract boolean from a checkbox property."""
    return bool((props.get(name) or {}).get("checkbox", False))


def _get_number(props: dict, name: str) -> float | None:
    """Extract number from a number property (None if not set)."""
    return (props.get(name) or {}).get("number")


def _get_date_start(props: dict, name: str) -> str | None:
    """Extract the start date string from a date property."""
    d = (props.get(name) or {}).get("date")
    return d.get("start") if d else None


def _get_url(props: dict, name: str) -> str | None:
    """Extract URL string from a url property."""
    return (props.get(name) or {}).get("url")


def _ts(iso: str | None) -> int | None:
    """Parse ISO datetime string to Unix timestamp."""
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _entity_and_sub(entity_raw: str | None) -> tuple[str, str | None]:
    """Map raw Notion entity string to KB (entity, sub_entity) tuple."""
    if not entity_raw:
        return "FNDR", None
    entity = _ENTITY_MAP.get(entity_raw, "FNDR")
    sub_entity = _SUB_ENTITY_MAP.get(entity_raw)
    return entity, sub_entity


def _format_contract_content(
    title: str,
    entity_raw: str,
    counterparty: str,
    contract_type: str | None,
    status: str | None,
    risk_flag: str | None,
    auto_renew: bool,
    annual_value: float | None,
    term_end: str | None,
    renewal_window: float | None,
    signed_date: str | None,
    effective_date: str | None,
    counterparty_contact: str | None,
    surviving_obligations: str | None,
    notes: str,
) -> str:
    """Build the chunkable text content for a contract Document."""
    lines = [f"[Contract] {title}", ""]
    lines.append(f"Entity: {entity_raw}")
    if counterparty:
        lines.append(f"Counterparty: {counterparty}")
    if contract_type:
        lines.append(f"Contract Type: {contract_type}")
    if status:
        lines.append(f"Status: {status}")
    lines.append(f"Risk Flag: {risk_flag or 'Standard'}")
    lines.append(f"Auto-renew: {'Yes' if auto_renew else 'No'}")
    if annual_value is not None:
        lines.append(f"Annual Value: ${annual_value:,.2f}")
    lines.append(f"Term End: {term_end or '(not set)'}")
    if renewal_window is not None:
        lines.append(f"Renewal Notice Window: {int(renewal_window)} days")
    if signed_date:
        lines.append(f"Signed Date: {signed_date}")
    if effective_date:
        lines.append(f"Effective Date: {effective_date}")
    if counterparty_contact:
        lines.append(f"Counterparty Contact: {counterparty_contact}")
    if surviving_obligations:
        lines.append(f"Surviving Obligations: {surviving_obligations}")
    if notes:
        lines.append("")
        lines.append("Notes:")
        lines.append(notes)
    return "\n".join(lines)


def _page_to_document(page: dict) -> "Document | None":
    """Convert a Notion page dict to a Document. Returns None if page is invalid."""
    page_id = page.get("id", "")
    page_url = page.get("url", "")
    last_edited_time = page.get("last_edited_time", "")
    created_time = page.get("created_time", "")

    props = page.get("properties", {})

    title = _get_title(props, "Title")
    if not title:
        return None

    entity_raw = _get_select(props, "Entity") or ""
    counterparty = _get_rich_text(props, "Counterparty")
    contract_type = _get_select(props, "Contract Type")
    status = _get_select(props, "Status")
    risk_flag = _get_select(props, "Risk Flag")
    auto_renew = _get_checkbox(props, "Auto-renew")
    annual_value = _get_number(props, "Annual Value")
    term_end = _get_date_start(props, "Term End")
    renewal_window = _get_number(props, "Renewal Notice Window (days)")
    signed_date = _get_date_start(props, "Signed Date")
    effective_date = _get_date_start(props, "Effective Date")
    counterparty_contact = _get_rich_text(props, "Counterparty Contact") or None
    surviving_obligations = _get_rich_text(props, "Surviving Obligations") or None
    notes = _get_rich_text(props, "Notes")
    linked_doc = _get_url(props, "Linked Document")

    entity, sub_entity = _entity_and_sub(entity_raw or None)

    content = _format_contract_content(
        title=title,
        entity_raw=entity_raw or entity,
        counterparty=counterparty,
        contract_type=contract_type,
        status=status,
        risk_flag=risk_flag,
        auto_renew=auto_renew,
        annual_value=annual_value,
        term_end=term_end,
        renewal_window=renewal_window,
        signed_date=signed_date,
        effective_date=effective_date,
        counterparty_contact=counterparty_contact,
        surviving_obligations=surviving_obligations,
        notes=notes,
    )

    date_modified = _ts(last_edited_time)
    date_created = _ts(created_time)
    deep_link = f"<{page_url}|{title}>" if page_url else title

    return Document(
        source="notion",
        source_id=f"notion:{page_id}",
        entity=entity,
        sub_entity=sub_entity,
        content=content,
        date_created=date_created,
        date_modified=date_modified,
        author="",
        title=title,
        deep_link=deep_link,
        metadata={
            "page_id": page_id,
            "entity_raw": entity_raw,
            "counterparty": counterparty,
            "contract_type": contract_type,
            "status": status,
            "risk_flag": risk_flag or "Standard",
            "auto_renew": auto_renew,
            "term_end": term_end,
            "annual_value": annual_value,
            "linked_document": linked_doc,
        },
    )


def sync_delta(last_sync_ts: int) -> Iterator[Document]:
    """Yield Documents for Notion pages edited since last_sync_ts.

    Uses Notion's server-side last_edited_time filter for efficiency.
    """
    since_iso = datetime.fromtimestamp(last_sync_ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    log.info("Notion sync_delta: querying pages edited after %s", since_iso)

    filter_body = {
        "timestamp": "last_edited_time",
        "last_edited_time": {"after": since_iso},
    }

    count = 0
    for page in _paginate_db(filter_body=filter_body):
        doc = _page_to_document(page)
        if doc:
            yield doc
            count += 1

    log.info("Notion sync_delta: yielded %d documents", count)


def backfill() -> Iterator[Document]:
    """Yield Documents for ALL pages in the Contracts & Renewals Registry.

    Used for initial population or full re-index. No timestamp filter.
    """
    log.info("Notion backfill: walking all pages in DB %s", _DB_ID)
    count = 0
    for page in _paginate_db():
        doc = _page_to_document(page)
        if doc:
            yield doc
            count += 1
    log.info("Notion backfill: yielded %d documents", count)
