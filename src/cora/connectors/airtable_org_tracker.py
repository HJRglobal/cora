"""Narrow, single-purpose Airtable READ path: the Org Remodel Tracker's
`Pending / Build Needs` table, for the propose-only decisions transcription.

WHY THIS IS A THIRD MODULE, when two Airtable connectors already exist. Both
existing boundaries are documented invariants with tests behind them, and this
read fits neither:

  * `airtable_client.py` is the DASHBOARD read surface, with a hard base allowlist
    pinned by test. The tracker is not a dashboard, so widening that allowlist to
    reach it would erase a real boundary.

    CORRECTION 2026-08-19 (D-051 lens-5): an earlier draft of this comment also
    claimed `AIRTABLE_API_KEY` "is scoped to those two bases and deliberately
    cannot reach this one". That is FALSE -- measured, the read-only PAT returns
    HTTP 200 on this base. The claim was inherited from
    airtable_training_log.py's header, where it is about WRITE access (a
    read-only PAT indeed cannot write) and was over-read as being about reads.
    The allowlist argument stands on its own; the credential argument does not,
    and a broadly-scoped PAT is a reason to keep the allowlist narrow, not to
    widen it.
  * `airtable_training_log.py` is the WRITE path, and its stated invariant is
    "ONE operation: create. No update, no delete, no list." Adding a list would
    break the sentence its own test reads.

So: one base, one table, ONE operation (list), GET-only by construction -- there
is no post/patch/delete in this file and no parameter that can point it at another
base or table. It prefers AIRTABLE_WRITE_API_KEY when set and falls back to the
read-only AIRTABLE_API_KEY -- which is the correct credential for a GET-only
module. Keying on the write PAT alone made this module dead on this host, where
that variable is not set at all.

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
    """A credential that can READ this base.

    Prefers the write PAT when present, but falls back to the read-only
    AIRTABLE_API_KEY -- which is the CORRECT credential for a GET-only module,
    and, measured 2026-08-19, reads this base fine:
        GET /v0/appAUZSQOCTnCO8yi/tbldM2EqIcho589Ql -> HTTP 200, records returned.
    The write PAT is not set on this host at all, so keying only on it made this
    reader -- and with it the entire decisions-transcription intake -- DEAD on
    arrival (D-051 lens-5 HIGH, caught before merge).
    """
    return (os.environ.get("AIRTABLE_WRITE_API_KEY", "").strip()
            or os.environ.get("AIRTABLE_API_KEY", "").strip())


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
