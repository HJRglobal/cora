#!/usr/bin/env python3
"""HubSpot Deal Task Sync -- create Asana tasks for deals entering Proposal stage.

Fires every 2 hours. Polls deal_last_stage table for Proposal-stage deals
and creates corresponding Asana tasks for the deal owner.

Usage (Windows Task Scheduler):
    python scripts/run_deal_task_sync.py [--dry-run]

Environment variables required:
    ASANA_PAT                    Asana personal access token
    HUBSPOT_PRIVATE_APP_TOKEN    HubSpot private app token (for deal URL)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

from cora.tools.asana_client import AsanaClientError, create_task  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("deal_task_sync")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROPOSAL_STAGE_ID = "3760204497"
DB_PATH    = _REPO_ROOT / "data" / "hubspot_deal_snapshots.db"
STATE_PATH = _REPO_ROOT / "data" / "state" / "deal_task_sync_state.json"
HS_MAP_FILE = _REPO_ROOT / "data" / "maps" / "slack-to-hubspot.yaml"

HUBSPOT_PORTAL_ID = "246351746"
RESYNC_WINDOW_DAYS = 7   # re-create task if deal re-enters Proposal after this many days


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _load_hs_map() -> dict[str, str]:
    """Return {hubspot_owner_id: asana_user_gid}."""
    try:
        with open(HS_MAP_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        mapping: dict[str, str] = {}
        for entry in data.get("users", []):
            owner_id = str(entry.get("hubspot_owner_id", ""))
            asana_gid = str(entry.get("asana_gid", "") or "")
            if owner_id and asana_gid:
                mapping[owner_id] = asana_gid
        return mapping
    except Exception as exc:
        log.warning("Failed to load HS map: %s", exc)
        return {}


def _load_asana_map_from_slack() -> dict[str, str]:
    """Return {hubspot_owner_id: asana_user_gid} by cross-referencing both yaml files."""
    slack_asana_file = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
    hs_slack_map: dict[str, str] = {}  # hubspot_owner_id -> slack_user_id
    asana_map: dict[str, str] = {}     # slack_user_id -> asana_gid

    try:
        with open(HS_MAP_FILE, encoding="utf-8") as f:
            hs_data = yaml.safe_load(f)
        for entry in hs_data.get("users", []):
            hs_id = str(entry.get("hubspot_owner_id", ""))
            slack_id = str(entry.get("slack_user_id", ""))
            if hs_id and slack_id:
                hs_slack_map[hs_id] = slack_id

        with open(slack_asana_file, encoding="utf-8") as f:
            sa_data = yaml.safe_load(f)
        for entry in sa_data.get("users", []):
            slack_id = str(entry.get("slack_user_id", ""))
            asana_gid = str(entry.get("asana_user_gid", ""))
            if slack_id and asana_gid:
                asana_map[slack_id] = asana_gid
    except Exception as exc:
        log.warning("Failed to build owner map: %s", exc)
        return {}

    # Cross-reference
    result: dict[str, str] = {}
    for hs_id, slack_id in hs_slack_map.items():
        if slack_id in asana_map:
            result[hs_id] = asana_map[slack_id]
    return result


def _deal_url(deal_id: str) -> str:
    return f"https://app.hubspot.com/contacts/{HUBSPOT_PORTAL_ID}/deal/{deal_id}/"


def _add_business_days(start: date, days: int) -> date:
    """Add `days` business days (Mon-Fri) to start date."""
    current = start
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:  # Mon=0 ... Fri=4
            added += 1
    return current


def _get_proposal_deals() -> list[dict[str, Any]]:
    """Query deal_last_stage for all Proposal-stage deals."""
    if not DB_PATH.exists():
        log.warning("deal_snapshots.db not found at %s", DB_PATH)
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT deal_id, deal_name, pipeline_id, stage_id, stage_name, amount, owner_id, last_seen_ts "
            "FROM deal_last_stage WHERE stage_id = ?",
            (PROPOSAL_STAGE_ID,),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as exc:
        log.error("DB query failed: %s", exc)
        return []


def _get_all_deals() -> list[dict[str, Any]]:
    """Query deal_last_stage for ALL deals (for state cleanup)."""
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT deal_id, stage_id FROM deal_last_stage"
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as exc:
        log.error("DB query for all deals failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> dict[str, int]:
    state = _load_state()
    owner_to_asana = _load_asana_map_from_slack()
    now_ts = int(time.time())

    proposal_deals = _get_proposal_deals()
    all_deals = _get_all_deals()

    # Cleanup: update state for deals no longer in Proposal
    current_stage_by_deal = {d["deal_id"]: d["stage_id"] for d in all_deals}
    for deal_id, deal_state in list(state.items()):
        current_stage = current_stage_by_deal.get(deal_id)
        if current_stage and current_stage != PROPOSAL_STAGE_ID:
            # Deal moved out of Proposal -- update state stage so re-entry creates new task
            if deal_state.get("stage_id") == PROPOSAL_STAGE_ID:
                state[deal_id]["stage_id"] = current_stage
                log.info("deal_task_sync: deal %s left Proposal -- updated state", deal_id)

    deals_checked = len(proposal_deals)
    tasks_created = 0
    skipped = 0

    for deal in proposal_deals:
        deal_id   = deal["deal_id"]
        deal_name = deal.get("deal_name") or f"Deal {deal_id}"
        owner_id  = str(deal.get("owner_id") or "")
        amount_raw = deal.get("amount") or "0"
        try:
            amount = float(amount_raw)
        except (ValueError, TypeError):
            amount = 0.0

        # Check if we already created a task for this deal in Proposal
        existing = state.get(deal_id)
        if existing and existing.get("stage_id") == PROPOSAL_STAGE_ID:
            synced_at = existing.get("synced_at", 0)
            if now_ts - synced_at < RESYNC_WINDOW_DAYS * 86400:
                skipped += 1
                log.debug("Skipping deal %s -- already synced within window", deal_id)
                continue

        asana_gid = owner_to_asana.get(owner_id)
        due_on = _add_business_days(date.today(), 3).isoformat()
        deal_url = _deal_url(deal_id)

        task_name  = f"Send proposal for {deal_name}"
        task_notes = (
            f"HubSpot deal auto-task (Cora). "
            f"Deal: {deal_url}. "
            f"Stage: Proposal. "
            f"Amount: ${amount:,.0f}."
        )

        log.info(
            "deal_task_sync: creating Asana task for deal_id=%s deal=%s owner=%s asana_gid=%s",
            deal_id, deal_name, owner_id, asana_gid or "(unmapped)",
        )

        if not dry_run:
            try:
                task = create_task(
                    name=task_name,
                    assignee_gid=asana_gid if asana_gid else None,
                    notes=task_notes,
                    due_on=due_on,
                )
                task_gid = task.get("gid", "unknown")
            except AsanaClientError as exc:
                log.error("Failed to create Asana task for deal %s: %s", deal_id, exc)
                continue
        else:
            task_gid = "DRY_RUN_GID"
            log.info(
                "[DRY RUN] Would create task: %r due=%s asana_gid=%s",
                task_name, due_on, asana_gid,
            )

        state[deal_id] = {
            "task_gid": task_gid,
            "synced_at": now_ts,
            "stage_id": PROPOSAL_STAGE_ID,
            "deal_name": deal_name,
        }
        tasks_created += 1

    # Always save state -- cleanup may have updated stage_id for deals that left Proposal
    _save_state(state)

    return {
        "deals_checked": deals_checked,
        "tasks_created": tasks_created,
        "skipped": skipped,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HubSpot Deal Task Sync")
    parser.add_argument("--dry-run", action="store_true", help="Log without creating tasks")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    log.info("deal_task_sync result: %s", result)
