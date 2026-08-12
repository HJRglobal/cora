"""Narrow, single-purpose Airtable WRITE path: one row into the Org Remodel
Tracker's Training Log, for the daily knowledge check.

WHY THIS IS NOT IN airtable_client.py. That module's first line is "READ-ONLY by
construction: the only network method is a paginated GET ... There are no
create/update/delete methods", and its hard base allowlist covers the two
dashboard bases. That read-only property is an invariant other code and its own
tests rely on; adding a create method for one feature would weaken it for every
caller. So the write lives here instead, scoped as tightly as it can be:

  * ONE base and ONE table, both pinned from data/maps/knowledge-check-airtable.yaml
    and re-verified against that map on every call -- there is no parameter that
    can point this at anything else.
  * ONE operation: create. No update, no delete, no list.
  * A SEPARATE credential (AIRTABLE_WRITE_API_KEY). The existing read-only
    AIRTABLE_API_KEY is scoped to two other bases and deliberately cannot reach
    this one, so a misconfiguration degrades to "not connected", never to a write
    with the wrong token.

FAIL-SOFT BY DESIGN (kickoff step 2.4): the known-answers write is the one that
matters; this mirror is best-effort. An unset token, an HTTP error, or a schema
drift returns (False, reason) and NEVER raises, so a failed mirror can never
abort a promote or lose the person's confirmed answer. The caller logs the
discrepancy.

FIELD IDS, NOT NAMES: writes against this base 400 on field names.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

_API_ROOT = "https://api.airtable.com/v0"
_HTTP_TIMEOUT = 15.0
_MAX_ATTEMPTS = 3          # one initial try + two retries on a transient failure
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Belt-and-braces pin. The map file supplies these, but a map edit must never be
# able to silently redirect writes at another base -- the value is compared, not
# trusted.
EXPECTED_BASE = "appAUZSQOCTnCO8yi"
EXPECTED_TABLE = "tbladeAQjMUlGOxhX"

_REQUIRED_FIELD_KEYS = ("session", "date", "person", "type", "outcome",
                        "feeds", "logged_by")


def _map_path() -> Path:
    return Path(os.environ.get("KNOWLEDGE_CHECK_AIRTABLE_MAP")
                or _REPO_ROOT / "data" / "maps" / "knowledge-check-airtable.yaml")


def load_map() -> dict[str, Any] | None:
    """The field map, or None if missing/invalid. Never raises."""
    try:
        import yaml
        raw = yaml.safe_load(_map_path().read_text(encoding="utf-8")) or {}
        fields = raw.get("fields") or {}
        if raw.get("base_id") != EXPECTED_BASE or raw.get("table_id") != EXPECTED_TABLE:
            log.error("airtable_training_log: map points at an unexpected base/table "
                      "-- refusing")
            return None
        missing = [k for k in _REQUIRED_FIELD_KEYS if not fields.get(k)]
        if missing:
            log.error("airtable_training_log: map is missing field ids: %s", missing)
            return None
        return raw
    except Exception:  # noqa: BLE001 -- fail-soft
        log.warning("airtable_training_log: map load failed", exc_info=True)
        return None


def _key() -> str:
    return os.environ.get("AIRTABLE_WRITE_API_KEY", "").strip()


def is_connected() -> bool:
    """True when a write PAT is configured (does not validate it)."""
    return bool(_key())


def log_knowledge_check(*, session: str, person: str, outcome: str,
                        date: str) -> tuple[bool, str]:
    """Append ONE Training Log row. Returns (ok, detail). Never raises.

    Retries only on transport errors and 5xx/429 -- a 4xx is a schema/permission
    problem that retrying cannot fix, so it fails fast with the reason.
    """
    if not is_connected():
        return False, "AIRTABLE_WRITE_API_KEY not set -- Training Log mirror is off"
    cfg = load_map()
    if cfg is None:
        return False, "Training Log field map unavailable"
    f = cfg["fields"]
    payload = {
        "records": [{"fields": {
            f["session"]: session,
            f["date"]: date,
            f["person"]: person,
            f["type"]: cfg.get("type_option", "Knowledge check"),
            f["outcome"]: outcome,
            f["feeds"]: cfg.get("feeds_option", "Cora KB"),
            f["logged_by"]: cfg.get("logged_by", "Cora (daily knowledge check)"),
        }}],
        # Required for the single-selects. NOTE: on the Type field this CREATES
        # the option on first write if it does not exist -- documented at the top
        # of the map file so it is a choice, not a surprise.
        "typecast": True,
    }
    url = f"{_API_ROOT}/{EXPECTED_BASE}/{EXPECTED_TABLE}"
    headers = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}

    last = "unknown error"
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
                resp = client.post(url, headers=headers, json=payload)
            if resp.status_code < 300:
                rec = ((resp.json() or {}).get("records") or [{}])[0]
                return True, rec.get("id", "created")
            body = (resp.text or "")[:200]
            last = f"HTTP {resp.status_code}: {body}"
            if resp.status_code < 500 and resp.status_code != 429:
                log.warning("airtable_training_log: not retrying %s", last)
                return False, last
        except Exception as exc:  # noqa: BLE001 -- fail-soft
            last = str(exc)
        log.warning("airtable_training_log: attempt %d/%d failed: %s",
                    attempt, _MAX_ATTEMPTS, last)
    return False, last
