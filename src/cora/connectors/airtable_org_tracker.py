"""Narrow, single-purpose Airtable READ path: the Org Remodel Tracker's
`Pending / Build Needs` table, for the propose-only decisions transcription.

WHY THIS IS A THIRD MODULE, when two Airtable connectors already exist. Both
existing boundaries are documented invariants with tests behind them, and this
read fits neither:

  * `airtable_client.py` is the DASHBOARD read surface: a hard base allowlist
    covering exactly two dashboard bases, pinned by
    test_airtable_allowed_bases_are_the_two_dashboards. Adding the tracker there
    would widen that boundary -- AND would not even work: `AIRTABLE_API_KEY` is
    scoped to those two bases and deliberately cannot reach this one, so the
    entry would be inert as well as wrong.
  * `airtable_training_log.py` is the WRITE path, and its stated invariant is
    "ONE operation: create. No update, no delete, no list." Adding a list would
    break the sentence its own test reads.

So: one base, one table, ONE operation (list), GET-only by construction -- there
is no post/patch/delete in this file and no parameter that can point it at another
base or table. It shares AIRTABLE_WRITE_API_KEY because that is the only
credential with access to this base; the name is about scope, not intent, and
nothing here writes.

FAIL-SOFT: an unset token, an HTTP error or a schema drift returns an empty,
`available=False` result and NEVER raises. The consumer is a propose-only script;
a failed read means "no proposal this run", never a wrong one.

cq-232fe6a541ff: this table is where five Open decisions sat past their gate date
while every Cora surface read only memory/decisions-pending.md.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

_API_ROOT = "https://api.airtable.com/v0"
_HTTP_TIMEOUT = 20.0
_PAGE_SIZE = 100
_MAX_PAGES = 20        # safety cap -> at most 2000 rows per call

#: Pinned, not parameterized. A caller cannot redirect this at another base/table.
BASE_ID = "appAUZSQOCTnCO8yi"          # Org Remodel Tracker
TABLE_ID = "tbldM2EqIcho589Ql"         # "Pending / Build Needs"


@dataclass
class TrackerResult:
    records: list[dict[str, Any]] = field(default_factory=list)
    available: bool = True
    error: str = ""


def _key() -> str:
    return os.environ.get("AIRTABLE_WRITE_API_KEY", "").strip()


def is_connected() -> bool:
    """True if a credential with access to this base is configured."""
    return bool(_key())


def list_pending_rows(fields: list[str] | None = None) -> TrackerResult:
    """Every row of `Pending / Build Needs`, each as its FIELDS dict. Never raises.

    `fields` restricts the returned columns (data minimization -- this table also
    holds build items and free-text notes this consumer has no business reading).
    An unknown field name makes Airtable 422, so on a 4xx the call is retried ONCE
    without the projection rather than failing the whole run on a column rename.
    """
    key = _key()
    if not key:
        return TrackerResult(available=False,
                             error="AIRTABLE_WRITE_API_KEY not set")

    url = f"{_API_ROOT}/{BASE_ID}/{TABLE_ID}"
    headers = {"Authorization": f"Bearer {key}"}

    def _fetch(use_fields: bool) -> tuple[list[dict[str, Any]], str]:
        out: list[dict[str, Any]] = []
        offset: str | None = None
        for _ in range(_MAX_PAGES):
            params: dict[str, Any] = {"pageSize": _PAGE_SIZE}
            if use_fields and fields:
                params["fields[]"] = fields
            if offset:
                params["offset"] = offset
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                resp = client.get(url, headers=headers, params=params)
            if resp.status_code >= 300:
                return [], f"HTTP {resp.status_code}: {(resp.text or '')[:200]}"
            body = resp.json() or {}
            for record in body.get("records") or []:
                if isinstance(record, dict):
                    out.append(record.get("fields") or {})
            offset = body.get("offset")
            if not offset:
                break
        return out, ""

    try:
        records, error = _fetch(use_fields=True)
        if error and fields:
            log.warning("airtable_org_tracker: projected read failed (%s) -- "
                        "retrying without the field list", error)
            records, error = _fetch(use_fields=False)
        if error:
            return TrackerResult(available=False, error=error)
        return TrackerResult(records=records)
    except Exception as exc:  # noqa: BLE001 -- a read never breaks its caller
        log.warning("airtable_org_tracker: read failed: %s", exc)
        return TrackerResult(available=False, error=f"{type(exc).__name__}: {exc}")
