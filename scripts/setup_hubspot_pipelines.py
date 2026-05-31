#!/usr/bin/env python3
"""Create the four entity pipelines (F3E, UFL, OSN, BDM) in a new HubSpot account.

Run against the NEW account AFTER creating it and generating a Private App token:

    uv run python scripts/setup_hubspot_pipelines.py --token <NEW_TOKEN>

What this does:
  1. Creates four deal pipelines with correct stages
  2. Fetches the new account's owner list
  3. Writes data/hubspot-migration/new-pipeline-ids.json

Next step after this script:
  - Fill in owner_map.new_owner_id values in your export JSON
  - Run: uv run python scripts/import_hubspot_data.py --token <NEW_TOKEN> --export <path>

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
_TIMEOUT = 15.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("setup_pipelines")


# ── HTTP helpers ────────────────────────────────────────────────────────────────

def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _get(token: str, path: str, params: dict | None = None) -> dict:
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(f"{_BASE}{path}", headers=_headers(token), params=params or {})
    if r.status_code != 200:
        raise RuntimeError(f"GET {path} → {r.status_code}: {r.text[:300]}")
    return r.json()


def _post(token: str, path: str, body: dict) -> dict:
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(f"{_BASE}{path}", headers=_headers(token), json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"POST {path} → {r.status_code}: {r.text[:300]}")
    return r.json()


# ── Pipeline definitions ────────────────────────────────────────────────────────

def _stage(label: str, probability: float, closed_won: bool = False, closed_lost: bool = False) -> dict:
    """Build a pipeline stage definition."""
    meta: dict = {"probability": str(probability), "isClosed": "false"}
    if closed_won:
        meta["isClosed"] = "true"
        meta["probability"] = "1.0"
    if closed_lost:
        meta["isClosed"] = "true"
        meta["probability"] = "0.0"
    return {"label": label, "metadata": meta}


_PIPELINES = [
    {
        "label": "F3E Retail",
        "displayOrder": 0,
        "stages": [
            _stage("Identify",   0.10),
            _stage("Outreach",   0.20),
            _stage("Sample Sent", 0.35),
            _stage("Qualified",  0.50),
            _stage("Proposal",   0.65),
            _stage("Negotiation", 0.80),
            _stage("Closed Won",  1.0,  closed_won=True),
            _stage("Closed Lost", 0.0,  closed_lost=True),
        ],
    },
    {
        "label": "UFL Sponsorships",
        "displayOrder": 1,
        "stages": [
            _stage("Identify",    0.10),
            _stage("Outreach",    0.25),
            _stage("Proposal",    0.50),
            _stage("Negotiation", 0.75),
            _stage("Closed Won",  1.0,  closed_won=True),
            _stage("Closed Lost", 0.0,  closed_lost=True),
        ],
    },
    {
        "label": "OSN",
        "displayOrder": 2,
        "stages": [
            _stage("Lead",            0.10),
            _stage("Qualified",       0.30),
            _stage("Proposal",        0.55),
            _stage("Contract Review", 0.75),
            _stage("Closed Won",      1.0,  closed_won=True),
            _stage("Closed Lost",     0.0,  closed_lost=True),
        ],
    },
    {
        "label": "BDM",
        "displayOrder": 3,
        "stages": [
            _stage("Discovery", 0.20),
            _stage("Proposal",  0.45),
            _stage("Contract",  0.75),
            _stage("Active",    1.0,  closed_won=True),
            _stage("Inactive",  0.0,  closed_lost=True),
            _stage("Closed Lost", 0.0, closed_lost=True),
        ],
    },
]


# ── Owners ─────────────────────────────────────────────────────────────────────

def fetch_owners(token: str) -> list[dict]:
    """Fetch all CRM owners (users) from the new account."""
    data = _get(token, "/crm/v3/owners", {"limit": 100})
    return data.get("results", [])


# ── Create pipelines ────────────────────────────────────────────────────────────

def create_pipeline(token: str, definition: dict) -> dict:
    """POST a new deal pipeline. Returns the created pipeline object."""
    return _post(token, "/crm/v3/pipelines/deals", definition)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Create HubSpot pipelines in new account")
    parser.add_argument("--token", required=True, help="New account Private App token")
    args = parser.parse_args()
    token: str = args.token

    log.info("=== HubSpot Pipeline Setup starting ===")

    # 1. Verify token / fetch owners
    log.info("[1/3] Fetching owners from new account...")
    try:
        owners = fetch_owners(token)
    except Exception as exc:
        log.error("Could not connect to new HubSpot account: %s", exc)
        log.error("Check that your token has crm.objects.owners.read and crm.objects.deals.write scopes.")
        return 1
    log.info("  Found %d owner(s):", len(owners))
    owner_rows: dict[str, dict] = {}
    for o in owners:
        oid = str(o.get("id", ""))
        email = o.get("email", "")
        name = f"{o.get('firstName', '')} {o.get('lastName', '')}".strip()
        log.info("    %s  %-30s  %s", oid, name, email)
        owner_rows[oid] = {"name": name, "email": email, "old_owner_id": ""}

    # 2. Create pipelines
    log.info("[2/3] Creating %d pipelines...", len(_PIPELINES))
    created: list[dict] = []
    for defn in _PIPELINES:
        label = defn["label"]
        try:
            result = create_pipeline(token, defn)
            pid = result.get("id", "")
            stages_out = result.get("stages", [])
            stage_map = {s["label"]: s["id"] for s in stages_out}
            log.info("  Created '%s'  id=%s  stages=%d", label, pid, len(stages_out))
            created.append({
                "pipeline_name": label,
                "pipeline_id": pid,
                "stages": stage_map,
            })
            time.sleep(0.3)
        except Exception as exc:
            log.error("  FAILED to create pipeline '%s': %s", label, exc)
            return 1

    # 3. Write output
    log.info("[3/3] Writing pipeline IDs file...")
    out_dir = _REPO_ROOT / "data" / "hubspot-migration"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "new-pipeline-ids.json"

    output = {
        "pipelines": created,
        "owners": owner_rows,
    }
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("  Written to %s", out_path)

    log.info("")
    log.info("NEXT STEPS:")
    log.info("  1. Open %s", out_dir / "export-*.json")
    log.info("  2. Match the new owner IDs above to the old owner_map entries")
    log.info("  3. Fill in each  new_owner_id  field in the export JSON")
    log.info("     (The owner IDs listed above go in new_owner_id;")
    log.info("      the old IDs are already in the export file.)")
    log.info("  4. Run: uv run python scripts/import_hubspot_data.py --token <NEW_TOKEN> --export <path>")
    log.info("=== Pipeline setup complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
