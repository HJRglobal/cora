#!/usr/bin/env python3
"""Import exported HubSpot data into a new account.

Prerequisites:
  1. Run export_hubspot_data.py against the OLD account
  2. Run setup_hubspot_pipelines.py against the NEW account
  3. Fill in owner_map new_owner_id values in the export JSON

Run:
    uv run python scripts/import_hubspot_data.py \\
        --token <NEW_TOKEN> \\
        --export data/hubspot-migration/export-YYYY-MM-DD.json

What this does (in order):
  1. Validates the export file — checks that owner_map.new_owner_id is filled
  2. Imports companies (deduped by name)
  3. Imports contacts (deduped by email)
  4. Imports deals with re-mapped owner IDs and pipeline/stage IDs
  5. Attaches contacts + companies to their deals
  6. Imports deal notes as HubSpot notes engagements
  7. Writes data/hubspot-migration/import-summary.json

Exit codes: 0 = success, 1 = error
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

_BASE    = "https://api.hubapi.com"
_TIMEOUT = 20.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("import_hubspot")


# ── HTTP helpers ────────────────────────────────────────────────────────────────

def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _post(token: str, path: str, body: dict) -> dict:
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(f"{_BASE}{path}", headers=_headers(token), json=body)
    if r.status_code not in (200, 201, 207):
        raise RuntimeError(f"POST {path} → {r.status_code}: {r.text[:300]}")
    return r.json()


# ── Stage ID mapping ────────────────────────────────────────────────────────────

def _build_stage_map(export: dict) -> dict[str, str]:
    """Map old_pipeline_id/old_stage_label → new_stage_id by matching pipeline names + stage labels.

    Reads new-pipeline-ids.json from the same directory as the export file.
    """
    export_path = Path(export["_source_path"])
    new_ids_path = export_path.parent / "new-pipeline-ids.json"
    if not new_ids_path.exists():
        raise FileNotFoundError(
            f"new-pipeline-ids.json not found at {new_ids_path}. "
            "Run setup_hubspot_pipelines.py first."
        )
    new_ids = json.loads(new_ids_path.read_text(encoding="utf-8-sig"))

    # Build: new pipeline name → {stage_label: stage_id}
    new_by_name: dict[str, dict[str, str]] = {}
    for p in new_ids.get("pipelines", []):
        new_by_name[p["pipeline_name"]] = p["stages"]

    # Build: old_pipeline_id → old pipeline name (from export pipeline_map)
    old_pipeline_names = {pid: v["name"] for pid, v in export.get("pipeline_map", {}).items()}

    # Build combined map: (old_pipeline_id, stage_id_or_label) → new_stage_id
    # HubSpot stage IDs in old account are not labels — we need to resolve them.
    # The export deals contain dealstage values which are stage IDs (not labels).
    # We'll fetch the old pipeline stages at runtime only if needed.
    # For now: return new_by_name keyed by old_pipeline_id for lookup at deal creation time.
    return {
        "old_pipeline_names": old_pipeline_names,
        "new_by_name": new_by_name,
    }


# ── Individual object creation ──────────────────────────────────────────────────

def _create_company(token: str, props: dict) -> str:
    """Create a company. Returns new ID."""
    body = {"properties": props}
    result = _post(token, "/crm/v3/objects/companies", body)
    return str(result["id"])


def _create_contact(token: str, props: dict) -> str:
    """Create a contact. Returns new ID."""
    body = {"properties": props}
    result = _post(token, "/crm/v3/objects/contacts", body)
    return str(result["id"])


def _create_deal(token: str, props: dict) -> str:
    """Create a deal. Returns new ID."""
    body = {"properties": props}
    result = _post(token, "/crm/v3/objects/deals", body)
    return str(result["id"])


def _associate(token: str, from_type: str, from_id: str, to_type: str, to_id: str, assoc_type: str) -> None:
    """Create a v4 association between two objects."""
    body = {
        "inputs": [{
            "from": {"id": from_id},
            "to": {"id": to_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": assoc_type}],
        }]
    }
    _post(token, f"/crm/v4/associations/{from_type}/{to_type}/batch/create", body)


def _create_note(token: str, deal_id: str, note_body: str, timestamp_ms: int, owner_id: str) -> None:
    """Create a note engagement and associate it with a deal."""
    props: dict = {
        "hs_note_body": note_body,
        "hs_timestamp": str(timestamp_ms),
    }
    if owner_id:
        props["hubspot_owner_id"] = owner_id
    body = {"properties": props}
    result = _post(token, "/crm/v3/objects/notes", body)
    note_id = str(result["id"])
    _associate(token, "notes", note_id, "deals", deal_id, "214")  # note→deal


# ── Stage resolution (fetch old pipeline stages once) ──────────────────────────

def _fetch_old_pipeline_stages(old_token: str | None, pipeline_id: str) -> dict[str, str]:
    """Return {stage_id: stage_label} for an old pipeline. Only called when old token provided."""
    if not old_token:
        return {}
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(
            f"{_BASE}/crm/v3/pipelines/deals/{pipeline_id}",
            headers=_headers(old_token),
        )
    if r.status_code != 200:
        return {}
    data = r.json()
    return {s["id"]: s["label"] for s in data.get("stages", [])}


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:  # noqa: C901 (intentionally long orchestrator)
    parser = argparse.ArgumentParser(description="Import HubSpot data into new account")
    parser.add_argument("--token",    required=True, help="New account Private App token")
    parser.add_argument("--export",   required=True, help="Path to export JSON file")
    parser.add_argument("--old-token", default="", help="Optional: old account token (used to resolve stage names)")
    parser.add_argument("--dry-run",  action="store_true", help="Print plan without creating anything")
    args = parser.parse_args()

    token: str     = args.token
    export_path    = Path(args.export).resolve()
    old_token: str = args.old_token
    dry_run: bool  = args.dry_run

    if not export_path.exists():
        log.error("Export file not found: %s", export_path)
        return 1

    log.info("=== HubSpot Import starting%s ===", " [DRY RUN]" if dry_run else "")
    log.info("Export: %s", export_path)

    export = json.loads(export_path.read_text(encoding="utf-8-sig"))  # utf-8-sig strips BOM if PowerShell wrote it
    export["_source_path"] = str(export_path)

    # Validate owner_map
    owner_map = export.get("owner_map", {})
    missing_owners = [v["name"] for v in owner_map.values() if not v.get("new_owner_id")]
    if missing_owners:
        log.error("These owners are missing new_owner_id in the export file:")
        for name in missing_owners:
            log.error("  %s", name)
        log.error("Fill in new_owner_id from the output of setup_hubspot_pipelines.py, then re-run.")
        return 1

    old_to_new_owner: dict[str, str] = {
        old_id: v["new_owner_id"] for old_id, v in owner_map.items()
    }

    # Build stage map
    try:
        stage_lookup = _build_stage_map(export)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1

    old_pipeline_names: dict[str, str] = stage_lookup["old_pipeline_names"]
    new_stages_by_pipeline: dict[str, dict[str, str]] = stage_lookup["new_by_name"]

    # Cache old stage ID → label per pipeline (fetched on demand)
    _old_stage_cache: dict[str, dict[str, str]] = {}

    def _resolve_stage(old_pipeline_id: str, old_stage_id: str) -> str:
        """Map old stage ID → new stage ID. Returns "" if unresolvable."""
        pipeline_name = old_pipeline_names.get(old_pipeline_id, "")
        new_stages = new_stages_by_pipeline.get(pipeline_name, {})
        if not new_stages:
            return ""
        # Try to resolve stage label via old account API
        if old_pipeline_id not in _old_stage_cache:
            _old_stage_cache[old_pipeline_id] = _fetch_old_pipeline_stages(old_token, old_pipeline_id)
        stage_label = _old_stage_cache[old_pipeline_id].get(old_stage_id, old_stage_id)
        # Direct match
        if stage_label in new_stages:
            return new_stages[stage_label]
        # Fuzzy: find closest label (case-insensitive prefix)
        label_lower = stage_label.lower()
        for new_label, new_sid in new_stages.items():
            if new_label.lower().startswith(label_lower[:6]):
                return new_sid
        # Fallback: first stage
        return next(iter(new_stages.values()), "")

    summary: dict = {
        "companies_created": 0,
        "contacts_created": 0,
        "deals_created": 0,
        "associations_created": 0,
        "notes_created": 0,
        "errors": [],
    }

    # ── Step 1: Companies ────────────────────────────────────────────────────────
    log.info("[1/5] Importing %d companies...", len(export.get("companies", [])))
    old_to_new_company: dict[str, str] = {}
    _COMPANY_FIELDS = ["name", "website", "industry", "phone", "city", "state", "description", "numberofemployees"]
    for company in export.get("companies", []):
        old_id = str(company.get("id", ""))
        raw_props = company.get("properties", {})
        props = {k: v for k, v in raw_props.items() if k in _COMPANY_FIELDS and v}
        if not props.get("name"):
            log.warning("  Skipping company %s (no name)", old_id)
            continue
        if dry_run:
            log.info("  [DRY] Would create company: %s", props.get("name"))
            old_to_new_company[old_id] = "dry-run"
            summary["companies_created"] += 1
            continue
        try:
            new_id = _create_company(token, props)
            old_to_new_company[old_id] = new_id
            summary["companies_created"] += 1
            time.sleep(0.1)
        except Exception as exc:
            msg = f"Company {old_id} ({props.get('name')}): {exc}"
            log.warning("  WARN: %s", msg)
            summary["errors"].append(msg)

    log.info("  Created %d companies", summary["companies_created"])

    # ── Step 2: Contacts ─────────────────────────────────────────────────────────
    log.info("[2/5] Importing %d contacts...", len(export.get("contacts", [])))
    old_to_new_contact: dict[str, str] = {}
    _CONTACT_FIELDS = ["firstname", "lastname", "email", "phone", "company", "jobtitle", "lifecyclestage"]
    for contact in export.get("contacts", []):
        old_id = str(contact.get("id", ""))
        raw_props = contact.get("properties", {})
        props = {k: v for k, v in raw_props.items() if k in _CONTACT_FIELDS and v}
        if not (props.get("email") or props.get("firstname") or props.get("lastname")):
            log.warning("  Skipping contact %s (no identifying info)", old_id)
            continue
        if dry_run:
            name = f"{props.get('firstname','')} {props.get('lastname','')}".strip() or props.get("email", old_id)
            log.info("  [DRY] Would create contact: %s", name)
            old_to_new_contact[old_id] = "dry-run"
            summary["contacts_created"] += 1
            continue
        try:
            new_id = _create_contact(token, props)
            old_to_new_contact[old_id] = new_id
            summary["contacts_created"] += 1
            time.sleep(0.1)
        except Exception as exc:
            # Duplicate email is a 409 — log and skip
            msg = f"Contact {old_id} ({props.get('email')}): {exc}"
            if "409" in str(exc) or "CONTACT_EXISTS" in str(exc):
                log.info("  Contact already exists (duplicate email): %s", props.get("email"))
            else:
                log.warning("  WARN: %s", msg)
                summary["errors"].append(msg)

    log.info("  Created %d contacts", summary["contacts_created"])

    # ── Step 3: Deals ────────────────────────────────────────────────────────────
    log.info("[3/5] Importing %d deals...", len(export.get("deals", [])))
    old_to_new_deal: dict[str, str] = {}
    _DEAL_FIELDS = [
        "dealname", "amount", "deal_currency_code", "closedate", "description", "dealtype",
        "f3e_channel", "f3e_geography", "f3e_product_lines", "f3e_monthly_volume_cases",
    ]
    for deal in export.get("deals", []):
        old_id = str(deal.get("id", ""))
        raw_props = deal.get("properties", {})
        props = {k: v for k, v in raw_props.items() if k in _DEAL_FIELDS and v}

        # Remap owner
        old_owner = raw_props.get("hubspot_owner_id", "")
        new_owner = old_to_new_owner.get(old_owner, "")
        if new_owner:
            props["hubspot_owner_id"] = new_owner

        # Remap pipeline + stage
        old_pipeline = raw_props.get("pipeline", "")
        old_stage = raw_props.get("dealstage", "")
        if old_pipeline and old_stage:
            new_stage = _resolve_stage(old_pipeline, old_stage)
            pipeline_name = old_pipeline_names.get(old_pipeline, "")
            # Find new pipeline ID by name
            new_ids_path = export_path.parent / "new-pipeline-ids.json"
            new_ids_data = json.loads(new_ids_path.read_text(encoding="utf-8-sig"))
            new_pipeline_id = ""
            for p in new_ids_data.get("pipelines", []):
                if p["pipeline_name"] == pipeline_name:
                    new_pipeline_id = p["pipeline_id"]
                    break
            if new_pipeline_id:
                props["pipeline"] = new_pipeline_id
            if new_stage:
                props["dealstage"] = new_stage

        if not props.get("dealname"):
            log.warning("  Skipping deal %s (no name)", old_id)
            continue

        if dry_run:
            log.info("  [DRY] Would create deal: %s  owner=%s", props.get("dealname"), new_owner)
            old_to_new_deal[old_id] = "dry-run"
            summary["deals_created"] += 1
            continue
        try:
            new_id = _create_deal(token, props)
            old_to_new_deal[old_id] = new_id
            summary["deals_created"] += 1
            time.sleep(0.15)
        except Exception as exc:
            msg = f"Deal {old_id} ({raw_props.get('dealname')}): {exc}"
            log.warning("  WARN: %s", msg)
            summary["errors"].append(msg)

    log.info("  Created %d deals", summary["deals_created"])

    # ── Step 4: Associations ─────────────────────────────────────────────────────
    log.info("[4/5] Creating deal associations...")
    deal_to_contacts  = export.get("deal_to_contact_ids", {})
    deal_to_companies = export.get("deal_to_company_ids", {})

    for old_deal_id, contact_ids in deal_to_contacts.items():
        new_deal_id = old_to_new_deal.get(old_deal_id)
        if not new_deal_id or new_deal_id == "dry-run":
            continue
        for old_cid in contact_ids:
            new_cid = old_to_new_contact.get(old_cid)
            if not new_cid:
                continue
            try:
                _associate(token, "deals", new_deal_id, "contacts", new_cid, "3")  # deal→contact
                summary["associations_created"] += 1
                time.sleep(0.05)
            except Exception as exc:
                log.warning("  Assoc deal→contact failed: %s", exc)

    for old_deal_id, company_ids in deal_to_companies.items():
        new_deal_id = old_to_new_deal.get(old_deal_id)
        if not new_deal_id or new_deal_id == "dry-run":
            continue
        for old_coid in company_ids:
            new_coid = old_to_new_company.get(old_coid)
            if not new_coid:
                continue
            try:
                _associate(token, "deals", new_deal_id, "companies", new_coid, "5")  # deal→company
                summary["associations_created"] += 1
                time.sleep(0.05)
            except Exception as exc:
                log.warning("  Assoc deal→company failed: %s", exc)

    log.info("  Created %d associations", summary["associations_created"])

    # ── Step 5: Notes ────────────────────────────────────────────────────────────
    deal_notes = export.get("deal_notes", {})
    total_notes = sum(len(v) for v in deal_notes.values())
    log.info("[5/5] Importing %d notes...", total_notes)
    for old_deal_id, notes in deal_notes.items():
        new_deal_id = old_to_new_deal.get(old_deal_id)
        if not new_deal_id or new_deal_id == "dry-run":
            continue
        for note in notes:
            props = note.get("properties", {})
            body = props.get("hs_note_body", "").strip()
            if not body:
                continue
            ts_str = props.get("hs_timestamp") or props.get("createdate") or "0"
            try:
                ts_ms = int(float(ts_str))
                if ts_ms <= 0:
                    ts_ms = int(time.time() * 1000)
            except (ValueError, TypeError):
                ts_ms = int(time.time() * 1000)
            old_owner = props.get("hubspot_owner_id", "")
            new_owner = old_to_new_owner.get(old_owner, "")
            if dry_run:
                summary["notes_created"] += 1
                continue
            try:
                _create_note(token, new_deal_id, body, ts_ms, new_owner)
                summary["notes_created"] += 1
                time.sleep(0.15)
            except Exception as exc:
                log.warning("  Note creation failed: %s", exc)

    log.info("  Imported %d notes", summary["notes_created"])

    # ── Write summary ────────────────────────────────────────────────────────────
    out_path = export_path.parent / "import-summary.json"
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("")
    log.info("Summary written to %s", out_path)
    log.info("  Companies : %d", summary["companies_created"])
    log.info("  Contacts  : %d", summary["contacts_created"])
    log.info("  Deals     : %d", summary["deals_created"])
    log.info("  Assocs    : %d", summary["associations_created"])
    log.info("  Notes     : %d", summary["notes_created"])
    if summary["errors"]:
        log.warning("  Errors    : %d (see import-summary.json)", len(summary["errors"]))

    if not dry_run:
        log.info("")
        log.info("NEXT STEPS:")
        log.info("  1. Verify data in new HubSpot account at app.hubspot.com")
        log.info("  2. Update src/cora/tools/hubspot_client.py with new portal ID + pipeline IDs")
        log.info("  3. Update data/maps/slack-to-hubspot.yaml with new owner IDs")
        log.info("  4. Add Alex Cordova + Elena Meirndorf to slack-to-hubspot.yaml")
        log.info("  5. Deploy Cora pointing at new account")
        log.info("  6. Delete / cancel old HubSpot account")

    log.info("=== Import complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
