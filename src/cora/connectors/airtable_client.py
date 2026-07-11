"""Read-only Airtable REST client for the Cora dashboard read layer.

READ-ONLY by construction: the only network method is a paginated
``GET /v0/{base}/{table}`` list. There are no create/update/delete methods.

A HARD base-ID allowlist restricts reads to the two dashboard bases; any other
base id is refused before a request is made. The read-only Personal Access
Token (``AIRTABLE_API_KEY``, scoped to just those two bases) is the real
boundary -- this allowlist is defense in depth.

Fail-soft (mirrors ``otterly_client``): a missing ``AIRTABLE_API_KEY`` or any
HTTP / parse error yields ``AirtableResult(available=False, error=...)`` and
NEVER raises, so a not-yet-configured PAT degrades to a clean "not connected"
in the calling tool.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

_API_ROOT = "https://api.airtable.com/v0"
_HTTP_TIMEOUT = 15.0
_PAGE_SIZE = 100          # Airtable per-page maximum
_MAX_PAGES = 30           # safety cap -> at most 3000 records per call

# HARD allowlist: the two dashboard bases. Any other base id is refused.
ALLOWED_BASES: frozenset[str] = frozenset(
    {
        "appwF6W6eVTvPFjct",  # F3 Creators & Ambassadors CRM
        "appxbEBjIBf8Wwlbd",  # [FNDR] Freelancer & Content Pipeline
    }
)


@dataclass
class AirtableResult:
    """Result of a list call. ``records`` is a list of each row's *fields* dict
    (keyed by field NAME, Airtable's default). ``available`` is False on any
    problem (missing key, disallowed base, HTTP/parse error)."""

    base_id: str
    table: str
    records: list[dict[str, Any]] = field(default_factory=list)
    available: bool = True
    error: str = ""


def _key() -> str:
    return os.environ.get("AIRTABLE_API_KEY", "").strip()


def is_connected() -> bool:
    """True if a PAT is configured (does not validate it)."""
    return bool(_key())


def list_records(
    base_id: str,
    table: str,
    *,
    fields: list[str] | None = None,
    max_records: int | None = None,
) -> AirtableResult:
    """List records from a table (all pages up to the safety cap). Never raises.

    ``fields`` restricts the returned columns (data minimization). Records come
    back keyed by field NAME: single-select -> str, multi-select -> list[str],
    number/currency/percent -> number, date -> ISO string, formula -> its value.
    """
    if base_id not in ALLOWED_BASES:
        log.warning("airtable: refused non-allowlisted base %r", base_id)
        return AirtableResult(
            base_id=base_id, table=table, available=False, error="base not in allowlist"
        )
    key = _key()
    if not key:
        return AirtableResult(
            base_id=base_id, table=table, available=False, error="AIRTABLE_API_KEY not set"
        )

    url = f"{_API_ROOT}/{base_id}/{table}"
    headers = {"Authorization": f"Bearer {key}"}
    out: list[dict[str, Any]] = []
    offset: str | None = None
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            for _ in range(_MAX_PAGES):
                params: dict[str, Any] = {"pageSize": _PAGE_SIZE}
                if fields:
                    params["fields[]"] = fields
                if offset:
                    params["offset"] = offset
                resp = client.get(url, headers=headers, params=params)
                resp.raise_for_status()
                data = resp.json()
                for rec in data.get("records", []):
                    out.append(rec.get("fields", {}) or {})
                    if max_records and len(out) >= max_records:
                        return AirtableResult(
                            base_id=base_id, table=table, records=out[:max_records]
                        )
                offset = data.get("offset")
                if not offset:
                    break
            else:
                log.warning(
                    "airtable: hit page cap (%d) base=%s table=%s", _MAX_PAGES, base_id, table
                )
    except Exception as exc:  # noqa: BLE001 -- fail-soft, never raise
        log.warning("airtable: list failed base=%s table=%s: %s", base_id, table, exc)
        return AirtableResult(base_id=base_id, table=table, available=False, error=str(exc))

    return AirtableResult(base_id=base_id, table=table, records=out)
