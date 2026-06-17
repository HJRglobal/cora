#!/usr/bin/env python3
"""HubSpot Deal Stage Monitor -- detect and notify on deal stage changes.

Every hour, snapshots all active deals from both HubSpot pipelines. Compares
current stage to the previous snapshot and posts Slack notifications for any
deals that changed stage.

Usage (called by Windows Task Scheduler -- see deployment/setup-hubspot-deal-monitor-task.ps1):
    python scripts/run_hubspot_deal_monitor.py

Options:
    --dry-run    Detect changes and log; don't post to Slack.

Environment variables required (already in .env if Cora is running):
    HUBSPOT_PRIVATE_APP_TOKEN    HubSpot private app token
    SLACK_BOT_TOKEN              For posting stage-change notifications

Database: data/hubspot_deal_snapshots.db (auto-created on first run)

See deployment/setup-hubspot-deal-monitor-task.ps1 to register the scheduled task.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

import httpx  # noqa: E402
import yaml  # noqa: E402

from cora.tools.hubspot_client import (  # noqa: E402
    PIPELINE_F3E_RETAIL,
    PIPELINE_UFL_OSN_BDM,
    HubSpotClientError,
    _PIPELINE_NAME_CACHE,
    _STAGE_NAME_CACHE,
    _deal_url,
    _refresh_pipeline_cache,
    get_deals_by_pipeline,
)

LOG_DIR = _REPO_ROOT / "logs"
_DB_PATH = _REPO_ROOT / "data" / "hubspot_deal_snapshots.db"
_HUBSPOT_MAP_PATH = _REPO_ROOT / "data" / "maps" / "slack-to-hubspot.yaml"

# Pipeline -> Slack channel routing
_PIPELINE_CHANNEL: dict[str, str] = {
    PIPELINE_F3E_RETAIL:  "#f3-leadership",
    PIPELINE_UFL_OSN_BDM: "#hjrg-leadership",
}

log = logging.getLogger("hubspot-deal-monitor")


# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    """Open (and initialize if needed) the deal snapshots SQLite database."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deal_snapshots (
            deal_id      TEXT NOT NULL,
            deal_name    TEXT,
            pipeline_id  TEXT,
            stage_id     TEXT,
            stage_name   TEXT,
            amount       TEXT,
            owner_id     TEXT,
            snapshot_ts  INTEGER NOT NULL,
            PRIMARY KEY (deal_id, snapshot_ts)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS deal_last_stage (
            deal_id      TEXT PRIMARY KEY,
            stage_id     TEXT,
            stage_name   TEXT,
            deal_name    TEXT,
            pipeline_id  TEXT,
            amount       TEXT,
            owner_id     TEXT,
            last_seen_ts INTEGER
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Owner name resolution
# ---------------------------------------------------------------------------

_owner_id_to_name: dict[str, str] | None = None  # module-level cache


def _load_owner_names() -> dict[str, str]:
    """Build hubspot_owner_id -> display_name map from slack-to-hubspot.yaml."""
    global _owner_id_to_name
    if _owner_id_to_name is not None:
        return _owner_id_to_name
    try:
        data = yaml.safe_load(_HUBSPOT_MAP_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("Could not load slack-to-hubspot.yaml: %s", exc)
        _owner_id_to_name = {}
        return _owner_id_to_name
    result: dict[str, str] = {}
    for entry in (data.get("users") or []):
        if not isinstance(entry, dict):
            continue
        owner_id = str(entry.get("hubspot_owner_id", "")).strip()
        name = (entry.get("display_name") or "").strip()
        if owner_id and name:
            result[owner_id] = name
    _owner_id_to_name = result
    return _owner_id_to_name


def _get_owner_name(owner_id: str | None) -> str:
    """Return display name for a HubSpot owner ID, or empty string."""
    if not owner_id:
        return ""
    return _load_owner_names().get(str(owner_id), "")


# ---------------------------------------------------------------------------
# Slack notification
# ---------------------------------------------------------------------------

def _post_stage_change(
    channel: str,
    deal_id: str,
    deal_name: str,
    old_stage: str,
    new_stage: str,
    amount: str | None,
    owner_id: str | None,
    dry_run: bool = False,
) -> None:
    """Post a deal stage change notification to Slack."""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.warning("SLACK_BOT_TOKEN not set -- skipping Slack notification")
        return

    url = _deal_url(deal_id)
    owner_name = _get_owner_name(owner_id)
    owner_str = f" | Owner: {owner_name}" if owner_name else ""

    try:
        amount_val = float(amount) if amount else 0.0
        amount_str = f"${amount_val:,.0f}"
    except (ValueError, TypeError):
        amount_str = amount or "$0"

    text = (
        f":arrows_counterclockwise: *Deal stage changed* -- <{url}|{deal_name}>\n"
        f"{old_stage} -> *{new_stage}*\n"
        f"Amount: {amount_str}{owner_str}"
    )

    log.info(
        "Stage change: %r  %r -> %r  channel=%s",
        deal_name, old_stage, new_stage, channel,
    )

    if dry_run:
        log.info("[DRY RUN] Would post to %s:\n%s", channel, text)
        return

    from cora.slack_egress import sanitize_text  # noqa: PLC0415 -- B1: raw POST bypasses the WebClient patch
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"channel": channel, "text": sanitize_text(text)},
            )
        data = r.json()
        if not data.get("ok"):
            log.warning("Slack post failed for %s: %s", channel, data.get("error"))
    except Exception as exc:
        log.error("Slack notification error for %s: %s", channel, exc)


# ---------------------------------------------------------------------------
# Core snapshot + diff logic
# ---------------------------------------------------------------------------

def snapshot_and_diff(dry_run: bool = False) -> dict[str, Any]:
    """Snapshot all active deals and post notifications for stage changes.

    Returns:
        {
            "deals_checked": int,
            "stage_changes": int,
            "notifications_sent": int,
        }
    """
    result: dict[str, Any] = {
        "deals_checked": 0,
        "stage_changes": 0,
        "notifications_sent": 0,
    }

    # Ensure pipeline/stage cache is warm
    try:
        if not _STAGE_NAME_CACHE:
            _refresh_pipeline_cache()
    except HubSpotClientError as exc:
        log.error("Could not refresh pipeline cache: %s", exc)
        return result

    now_ts = int(time.time())

    conn = _get_db()
    try:
        for pipeline_id in (PIPELINE_F3E_RETAIL, PIPELINE_UFL_OSN_BDM):
            channel = _PIPELINE_CHANNEL.get(pipeline_id, "#hjrg-leadership")

            try:
                deals = get_deals_by_pipeline(pipeline_id)
            except HubSpotClientError as exc:
                log.error("Could not fetch deals for pipeline %s: %s", pipeline_id, exc)
                continue

            for deal in deals:
                deal_id = str(deal.get("id", ""))
                if not deal_id:
                    continue

                props = deal.get("properties") or {}
                deal_name = (props.get("dealname") or "(unnamed)").strip()
                stage_id = str(props.get("dealstage") or "")
                stage_name = _STAGE_NAME_CACHE.get(stage_id, stage_id)
                amount = props.get("amount") or ""
                owner_id = str(props.get("hubspot_owner_id") or "")

                result["deals_checked"] += 1

                # Insert snapshot row
                conn.execute(
                    """
                    INSERT OR REPLACE INTO deal_snapshots
                    (deal_id, deal_name, pipeline_id, stage_id, stage_name, amount, owner_id, snapshot_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (deal_id, deal_name, pipeline_id, stage_id, stage_name, amount, owner_id, now_ts),
                )

                # Check for stage change
                row = conn.execute(
                    "SELECT stage_id, stage_name FROM deal_last_stage WHERE deal_id = ?",
                    (deal_id,),
                ).fetchone()

                if row is not None:
                    old_stage_id = row["stage_id"]
                    old_stage_name = row["stage_name"] or _STAGE_NAME_CACHE.get(old_stage_id, old_stage_id)
                    if old_stage_id != stage_id:
                        # Stage changed -- notify
                        result["stage_changes"] += 1
                        _post_stage_change(
                            channel=channel,
                            deal_id=deal_id,
                            deal_name=deal_name,
                            old_stage=old_stage_name,
                            new_stage=stage_name,
                            amount=amount,
                            owner_id=owner_id,
                            dry_run=dry_run,
                        )
                        if not dry_run:
                            result["notifications_sent"] += 1
                        else:
                            result["notifications_sent"] += 1  # count dry-run too

                # Upsert last-known stage
                conn.execute(
                    """
                    INSERT OR REPLACE INTO deal_last_stage
                    (deal_id, stage_id, stage_name, deal_name, pipeline_id, amount, owner_id, last_seen_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (deal_id, stage_id, stage_name, deal_name, pipeline_id, amount, owner_id, now_ts),
                )

        conn.commit()
    finally:
        conn.close()

    log.info(
        "Deal monitor: checked=%d changes=%d notifications=%d",
        result["deals_checked"],
        result["stage_changes"],
        result["notifications_sent"],
    )
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"cora-{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect changes and log; don't post to Slack",
    )
    args = parser.parse_args()

    _setup_logging()
    log.info("=" * 60)
    log.info("HubSpot deal monitor starting (dry_run=%s)", args.dry_run)

    try:
        result = snapshot_and_diff(dry_run=args.dry_run)
    except Exception as exc:
        log.exception("Deal monitor crashed: %s", exc)
        return 1

    log.info(
        "Done: deals_checked=%d stage_changes=%d notifications_sent=%d",
        result["deals_checked"],
        result["stage_changes"],
        result["notifications_sent"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
