"""Read-only Airtable REST client for the Cora dashboard read layer.

READ-ONLY by construction: the only network method is a paginated
``GET /v0/{base}/{table}`` list. There are no create/update/delete methods.

A HARD base-ID allowlist restricts reads to the two dashboard bases; any other
base id is refused before a request is made. The read-only Personal Access
Token (``AIRTABLE_API_KEY``) is BROADER than this list -- measured 2026-08-19, it
returns HTTP 200 on bases that are not dashboards, including the Org Remodel
Tracker. So THIS ALLOWLIST is the real boundary for this surface, not defense in
depth behind a narrow credential. Treat an addition accordingly.

Fail-soft (mirrors ``otterly_client``): a missing ``AIRTABLE_API_KEY`` or any
HTTP / parse error yields ``AirtableResult(available=False, error=...)`` and
NEVER raises, so a not-yet-configured PAT degrades to a clean "not connected"
in the calling tool.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

_API_ROOT = "https://api.airtable.com/v0"
_HTTP_TIMEOUT = 15.0
_PAGE_SIZE = 100          # Airtable per-page maximum
_MAX_PAGES = 30           # safety cap -> at most 3000 records per call

# HARD allowlist: the DASHBOARD bases. Any other base id is refused.
ALLOWED_BASES: frozenset[str] = frozenset(
    {
        "appwF6W6eVTvPFjct",  # F3 Creators & Ambassadors CRM
        "appxbEBjIBf8Wwlbd",  # [FNDR] Freelancer & Content Pipeline
        # [F3E] Production Pipeline (2026-08-19, cq-fe9ec84a5ca2). A dashboard
        # base like the two above, so this is the right home for it -- unlike the
        # Org Remodel Tracker (see the note below). Its reader is PRICE-FREE by
        # construction: a fixed non-cost column projection plus a value screen on
        # everything rendered. NOTE the PAT must be granted read on this base;
        # until it is, the tool degrades to "isn't connected yet".
        "app1hWKmTAnvp09rR",
    }
)
# DO NOT add the Org Remodel Tracker (appAUZSQOCTnCO8yi) here. This module's
# allowlist is the DASHBOARD read surface; the tracker is not a dashboard, and it is
# read through connectors/airtable_org_tracker.py (one base, one table, GET-only).
# Pinned by test.
#
# CORRECTION 2026-08-19 (D-051 lens-5): an earlier version of this note also
# claimed the PAT "is scoped to the two bases above and cannot reach the tracker at
# all", so an entry here would be inert. MEASURED FALSE -- the read-only PAT
# returns HTTP 200 on the tracker base. The credential does not enforce this
# boundary; this list does.


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


class _UnknownFieldError(Exception):
    """Internal: an Airtable 4xx caused by an unknown field name in fields[]."""


def _looks_like_unknown_field(response) -> bool:
    try:
        return "UNKNOWN_FIELD_NAME" in (response.text or "").upper()
    except Exception:  # noqa: BLE001
        return False


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

    # A table NAME is as valid as a tbl id in this path, and the F3E production
    # base's names contain spaces ("Run Items"). quote() is a no-op on the tbl
    # ids every other caller passes, so this widens what works without changing
    # any existing request by a byte.
    url = f"{_API_ROOT}/{base_id}/{quote(str(table), safe='')}"
    headers = {"Authorization": f"Bearer {key}"}

    def _fetch(use_fields: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset: str | None = None
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            for _ in range(_MAX_PAGES):
                params: dict[str, Any] = {"pageSize": _PAGE_SIZE}
                if use_fields and fields:
                    params["fields[]"] = fields
                if offset:
                    params["offset"] = offset
                resp = client.get(url, headers=headers, params=params)
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    r = exc.response
                    if use_fields and fields and r is not None \
                            and r.status_code in (400, 422) and _looks_like_unknown_field(r):
                        raise _UnknownFieldError(str(exc)) from exc
                    raise
                data = resp.json()
                for rec in data.get("records", []):
                    out.append(rec.get("fields", {}) or {})
                    if max_records and len(out) >= max_records:
                        return out[:max_records]
                offset = data.get("offset")
                if not offset:
                    break
            else:
                log.warning(
                    "airtable: hit page cap (%d) base=%s table=%s", _MAX_PAGES, base_id, table
                )
        return out

    try:
        records = _fetch(use_fields=True)
    except _UnknownFieldError as exc:
        # A field name drifted (rename / casing). Recover by fetching all columns
        # and letting the formatter pick the keys it knows -- do NOT surface this
        # recoverable schema drift as a misleading "not connected".
        log.warning(
            "airtable: unknown field in %s/%s (%s) -- retrying without field restriction",
            base_id, table, exc,
        )
        try:
            records = _fetch(use_fields=False)
        except Exception as exc2:  # noqa: BLE001 -- fail-soft
            log.warning("airtable: fieldless retry failed base=%s table=%s: %s", base_id, table, exc2)
            return AirtableResult(base_id=base_id, table=table, available=False, error=str(exc2))
    except Exception as exc:  # noqa: BLE001 -- fail-soft, never raise
        log.warning("airtable: list failed base=%s table=%s: %s", base_id, table, exc)
        return AirtableResult(base_id=base_id, table=table, available=False, error=str(exc))

    return AirtableResult(base_id=base_id, table=table, records=records)
