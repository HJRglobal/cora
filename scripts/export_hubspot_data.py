#!/usr/bin/env python3
"""Export all valuable data from the current HubSpot account for migration.

Exports:
  - All deals (both pipelines, open + closed)
  - Contacts associated with those deals ONLY  (skips the ~450 spam contacts)
  - Companies associated with those deals
  - Notes/activities attached to deals

Output: data/hubspot-migration/export-YYYY-MM-DD.json

Run against the OLD account BEFORE deleting it:
    uv run python scripts/export_hubspot_data.py

Exit codes: 0 = success, 1 = error
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

_BASE    = "https://api.hubapi.com"
_TIMEOUT = 15.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("export_hubspot")


# ── Auth ───────────────────────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN", "")
    if not token:
        log.error("HUBSPOT_PRIVATE_APP_TOKEN not set")
        sys.exit(1)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _get(path: str, params: dict | None = None) -> dict:
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(f"{_BASE}{path}", headers=_headers(), params=params or {})
    if r.status_code != 200:
        raise RuntimeError(f"GET {path} → {r.status_code}: {r.text[:200]}")
    return r.json()


def _post(path: str, body: dict) -> dict:
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(f"{_BASE}{path}", headers=_headers(), json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"POST {path} → {r.status_code}: {r.text[:200]}")
    return r.json()


# ── Deals ──────────────────────────────────────────────────────────────────────

_DEAL_PROPERTIES = [
    "dealname", "amount", "deal_currency_code", "dealstage", "pipeline",
    "closedate", "createdate", "hubspot_owner_id", "hs_lastmodifieddate",
    "description", "dealtype",
    # F3E custom properties
    "f3e_channel", "f3e_geography", "f3e_product_lines", "f3e_monthly_volume_cases",
]


def fetch_all_deals() -> list[dict]:
    """Fetch all deals across all pipelines with full pagination."""
    all_deals: list[dict] = []
    after: str | None = None

    while True:
        body: dict = {
            "properties": _DEAL_PROPERTIES,
            "sorts": [{"propertyName": "createdate", "direction": "ASCENDING"}],
            "limit": 100,
        }
        if after:
            body["after"] = after

        data = _post("/crm/v3/objects/deals/search", body)
        results = data.get("results", []) or []
        all_deals.extend(results)
        log.info("  Deals fetched so far: %d", len(all_deals))

        after = (data.get("paging") or {}).get("next", {}).get("after")
        if not after or not results:
            break
        time.sleep(0.2)

    return all_deals


# ── Associations ───────────────────────────────────────────────────────────────

def batch_get_associations(from_type: str, to_type: str, object_ids: list[str]) -> dict[str, list[str]]:
    """Batch-fetch associations. Returns {from_id: [to_id, ...]}."""
    result: dict[str, list[str]] = {}
    chunk_size = 100

    for i in range(0, len(object_ids), chunk_size):
        chunk = object_ids[i:i + chunk_size]
        body = {"inputs": [{"id": oid} for oid in chunk]}
        try:
            data = _post(f"/crm/v4/associations/{from_type}/{to_type}/batch/read", body)
            for item in (data.get("results") or []):
                from_id = str(item.get("from", {}).get("id", ""))
                to_ids = [str(a.get("toObjectId", "")) for a in (item.get("to") or [])]
                if from_id and to_ids:
                    result[from_id] = to_ids
        except Exception as exc:
            log.warning("Association batch failed (%s→%s): %s", from_type, to_type, exc)
        time.sleep(0.15)

    return result


# ── Contacts / Companies ───────────────────────────────────────────────────────

_CONTACT_PROPERTIES = [
    "firstname", "lastname", "email", "phone", "company",
    "jobtitle", "hs_lead_status", "lifecyclestage", "createdate",
]

_COMPANY_PROPERTIES = [
    "name", "website", "industry", "phone", "city", "state",
    "description", "numberofemployees", "createdate",
]


def batch_fetch_objects(object_type: str, object_ids: list[str], properties: list[str]) -> list[dict]:
    """Batch-fetch CRM objects by ID."""
    all_results: list[dict] = []
    chunk_size = 100

    for i in range(0, len(object_ids), chunk_size):
        chunk = object_ids[i:i + chunk_size]
        body = {
            "inputs": [{"id": oid} for oid in chunk],
            "properties": properties,
        }
        try:
            data = _post(f"/crm/v3/objects/{object_type}/batch/read", body)
            all_results.extend(data.get("results") or [])
        except Exception as exc:
            log.warning("Batch fetch %s failed: %s", object_type, exc)
        time.sleep(0.15)

    return all_results


# ── Notes ──────────────────────────────────────────────────────────────────────

_NOTE_PROPERTIES = [
    "hs_note_body", "hs_timestamp", "hubspot_owner_id", "createdate",
]


def fetch_notes_for_deals(deal_ids: list[str]) -> dict[str, list[dict]]:
    """Fetch notes associated with each deal. Returns {deal_id: [note, ...]}."""
    deal_to_note_ids = batch_get_associations("deals", "notes", deal_ids)

    all_note_ids: list[str] = []
    for note_ids in deal_to_note_ids.values():
        all_note_ids.extend(note_ids)
    all_note_ids = list(set(all_note_ids))

    if not all_note_ids:
        return {}

    note_objects = batch_fetch_objects("notes", all_note_ids, _NOTE_PROPERTIES)
    note_by_id = {str(n["id"]): n for n in note_objects}

    result: dict[str, list[dict]] = {}
    for deal_id, note_ids in deal_to_note_ids.items():
        result[deal_id] = [note_by_id[nid] for nid in note_ids if nid in note_by_id]

    return result


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    log.info("=== HubSpot Export starting ===")

    # 1. Fetch all deals
    log.info("[1/5] Fetching all deals...")
    deals = fetch_all_deals()
    log.info("  Found %d deals total", len(deals))

    deal_ids = [str(d["id"]) for d in deals]

    # 2. Get deal → contact / company associations
    log.info("[2/5] Fetching deal associations (contacts + companies)...")
    deal_to_contacts = batch_get_associations("deals", "contacts", deal_ids)
    deal_to_companies = batch_get_associations("deals", "companies", deal_ids)

    all_contact_ids = list({cid for cids in deal_to_contacts.values() for cid in cids})
    all_company_ids = list({cid for cids in deal_to_companies.values() for cid in cids})
    log.info(
        "  %d deal-linked contacts, %d deal-linked companies",
        len(all_contact_ids), len(all_company_ids),
    )

    # 3. Fetch those contacts + companies (NOT the 450+ spam contacts)
    log.info("[3/5] Fetching deal-linked contacts and companies...")
    contacts = batch_fetch_objects("contacts", all_contact_ids, _CONTACT_PROPERTIES) if all_contact_ids else []
    companies = batch_fetch_objects("companies", all_company_ids, _COMPANY_PROPERTIES) if all_company_ids else []
    log.info("  %d contacts, %d companies", len(contacts), len(companies))

    # 4. Fetch notes on deals
    log.info("[4/5] Fetching deal notes/activities...")
    deal_notes = fetch_notes_for_deals(deal_ids)
    total_notes = sum(len(v) for v in deal_notes.values())
    log.info("  %d notes across %d deals", total_notes, len(deal_notes))

    # 5. Write output
    log.info("[5/5] Writing export file...")
    output = {
        "exported_at": date.today().isoformat(),
        "portal_id": "243870963",
        "summary": {
            "deals": len(deals),
            "contacts": len(contacts),
            "companies": len(companies),
            "notes": total_notes,
        },
        "owner_map": {
            "160459333": {"name": "Harrison Rogers", "email": "harrison@hjrglobal.com", "new_owner_id": ""},
            "162944825": {"name": "Tommy Anderson",  "email": "tommy@f3energy.com",    "new_owner_id": ""},
            "160262948": {"name": "Alex Cordova",    "email": "alex@hjrglobal.com",    "new_owner_id": ""},
            "83346026":  {"name": "Matt Petrovich",  "email": "matt@hjrglobal.com",    "new_owner_id": ""},
            "160454475": {"name": "Elena Meirndorf", "email": "elena@f3energy.com",    "new_owner_id": ""},
        },
        "pipeline_map": {
            "2234421978": {"name": "F3E Retail",       "new_pipeline_id": ""},
            "2242250445": {"name": "UFL Sponsorships", "new_pipeline_id": ""},
        },
        "deals": deals,
        "contacts": contacts,
        "companies": companies,
        "deal_to_contact_ids": deal_to_contacts,
        "deal_to_company_ids": deal_to_companies,
        "deal_notes": deal_notes,
    }

    out_dir = _REPO_ROOT / "data" / "hubspot-migration"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"export-{date.today().isoformat()}.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    log.info("Export written to %s", out_path)
    log.info("")
    log.info("NEXT STEPS:")
    log.info("  1. Create new HubSpot account at app.hubspot.com (Sales Hub Starter)")
    log.info("  2. Set timezone: Settings → Account Defaults → Time Zone → America/Phoenix")
    log.info("  3. Generate a Private App token with crm.objects.* scopes")
    log.info("  4. Run: uv run python scripts/setup_hubspot_pipelines.py --token <NEW_TOKEN>")
    log.info("  5. Fill in owner_map new_owner_id values in the export file")
    log.info("  6. Run: uv run python scripts/import_hubspot_data.py --token <NEW_TOKEN>")
    log.info("=== Export complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
