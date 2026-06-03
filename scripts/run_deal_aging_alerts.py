#!/usr/bin/env python3
"""Deal Aging Alerts -- DM HubSpot deal owners when deals stall in a stage.

Every night, checks all open deals from both HubSpot pipelines. If a deal has
been in its current stage longer than the threshold for that stage, sends a
Slack DM to the deal owner (or posts to #hjrg-leadership as fallback).

Throttle: a deal won't re-alert within 3 days (259200 seconds).
Throttle state persists in data/deal_aging_throttle.json.

Usage (called by Windows Task Scheduler):
    python scripts/run_deal_aging_alerts.py [--dry-run]

Environment variables required:
    HUBSPOT_PRIVATE_APP_TOKEN    HubSpot private app token
    SLACK_BOT_TOKEN              For sending DMs
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

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
_THROTTLE_PATH = _REPO_ROOT / "data" / "deal_aging_throttle.json"
_HUBSPOT_MAP_PATH = _REPO_ROOT / "data" / "maps" / "slack-to-hubspot.yaml"
_FALLBACK_CHANNEL = "#hjrg-leadership"
_THROTTLE_SECONDS = 259200  # 3 days

# Per-stage age thresholds (days before alert fires)
STAGE_THRESHOLDS: dict[str, int] = {
    "Identify":    14,
    "Outreach":    10,
    "Sample Sent": 7,
    "Qualified":   21,
    "Proposal":    14,
    "Negotiation": 7,
}
DEFAULT_THRESHOLD = 21  # for any stage not listed

log = logging.getLogger("deal-aging-alerts")


# ---------------------------------------------------------------------------
# Throttle state
# ---------------------------------------------------------------------------

def _load_throttle() -> dict[str, float]:
    """Load the throttle JSON. Returns empty dict if file doesn't exist."""
    if not _THROTTLE_PATH.exists():
        return {}
    try:
        return json.loads(_THROTTLE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not load throttle file: %s", exc)
        return {}


def _save_throttle(state: dict[str, float]) -> None:
    """Persist the throttle state to disk."""
    _THROTTLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _THROTTLE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _is_throttled(deal_id: str, state: dict[str, float], now_ts: float) -> bool:
    """Return True if this deal was alerted within the throttle window."""
    last_ts = state.get(str(deal_id))
    if last_ts is None:
        return False
    return (now_ts - last_ts) < _THROTTLE_SECONDS


# ---------------------------------------------------------------------------
# Owner -> Slack user ID resolution
# ---------------------------------------------------------------------------

_owner_to_slack: dict[str, str] | None = None


def _load_owner_to_slack() -> dict[str, str]:
    """Build hubspot_owner_id -> slack_user_id map from slack-to-hubspot.yaml."""
    global _owner_to_slack
    if _owner_to_slack is not None:
        return _owner_to_slack
    try:
        data = yaml.safe_load(_HUBSPOT_MAP_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("Could not load slack-to-hubspot.yaml: %s", exc)
        _owner_to_slack = {}
        return _owner_to_slack
    result: dict[str, str] = {}
    for entry in (data.get("users") or []):
        if not isinstance(entry, dict):
            continue
        owner_id = str(entry.get("hubspot_owner_id", "")).strip()
        slack_id = (entry.get("slack_user_id") or "").strip()
        if owner_id and slack_id:
            result[owner_id] = slack_id
    _owner_to_slack = result
    return _owner_to_slack


def _get_slack_id_for_owner(owner_id: str | None) -> str | None:
    """Return Slack user ID for a HubSpot owner ID, or None if not mapped."""
    if not owner_id:
        return None
    return _load_owner_to_slack().get(str(owner_id))


# ---------------------------------------------------------------------------
# Age calculation
# ---------------------------------------------------------------------------

def _parse_hs_date(value: str | None) -> float | None:
    """Parse a HubSpot ISO-8601 / epoch-ms date string to a Unix timestamp.

    HubSpot returns dates as ISO strings ('2026-01-15T12:00:00.000Z') or
    epoch-millisecond strings ('1705320000000'). Returns None if unparseable.
    """
    if not value:
        return None
    # Try epoch-ms integer
    if value.isdigit():
        ts = int(value) / 1000.0
        return ts
    # Try ISO-8601
    try:
        # Strip trailing Z for fromisoformat compatibility
        iso = value.rstrip("Z").replace("T", " ").split(".")[0]
        dt = datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _deal_age_days(props: dict[str, Any], now_ts: float) -> float:
    """Return the number of days since the deal was last modified.

    Falls back to createdate if hs_lastmodifieddate is unavailable.
    """
    for field in ("hs_lastmodifieddate", "createdate"):
        raw = props.get(field)
        ts = _parse_hs_date(raw)
        if ts is not None:
            return (now_ts - ts) / 86400.0
    return 0.0


def _get_threshold(stage_name: str) -> int:
    """Return the alert threshold (days) for a stage name."""
    return STAGE_THRESHOLDS.get(stage_name, DEFAULT_THRESHOLD)


# ---------------------------------------------------------------------------
# Slack DM
# ---------------------------------------------------------------------------

def _build_alert_text(
    deal_name: str,
    deal_id: str,
    stage_name: str,
    age_days: float,
    threshold: int,
    amount: str | None,
    pipeline_name: str,
) -> str:
    """Build the Slack alert message for an aging deal."""
    try:
        amount_str = f"${float(amount):,.0f}" if amount else "N/A"
    except (ValueError, TypeError):
        amount_str = amount or "N/A"
    url = _deal_url(deal_id)
    age_int = int(age_days)
    return (
        f":hourglass_flowing_sand: *Deal aging alert* -- <{url}|{deal_name}>\n"
        f"Stage: *{stage_name}* | In stage: *{age_int}d* (threshold: {threshold}d)\n"
        f"Amount: {amount_str} | Pipeline: {pipeline_name}\n"
        f"Recommended: Move forward or add a note with the latest status."
    )


def _send_slack_dm(
    recipient_id: str,
    text: str,
    dry_run: bool = False,
) -> bool:
    """Send a Slack DM to a user ID. Returns True on success."""
    import httpx

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.warning("SLACK_BOT_TOKEN not set -- cannot send DM")
        return False

    if dry_run:
        log.info("[DRY RUN] Would DM %s:\n%s", recipient_id, text)
        return True

    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"channel": recipient_id, "text": text},
            )
        data = r.json()
        if data.get("ok"):
            log.info("DM sent to %s", recipient_id)
            return True
        log.warning("Slack DM to %s failed: %s", recipient_id, data.get("error"))
        return False
    except Exception as exc:
        log.error("Slack DM error for %s: %s", recipient_id, exc)
        return False


def _send_fallback_channel(text: str, dry_run: bool = False) -> bool:
    """Post to #hjrg-leadership as a fallback when owner has no Slack mapping."""
    import httpx

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.warning("SLACK_BOT_TOKEN not set -- cannot post to fallback channel")
        return False

    if dry_run:
        log.info("[DRY RUN] Would post to %s:\n%s", _FALLBACK_CHANNEL, text)
        return True

    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"channel": _FALLBACK_CHANNEL, "text": text},
            )
        data = r.json()
        if data.get("ok"):
            log.info("Fallback post to %s sent", _FALLBACK_CHANNEL)
            return True
        log.warning("Fallback post to %s failed: %s", _FALLBACK_CHANNEL, data.get("error"))
        return False
    except Exception as exc:
        log.error("Fallback channel post error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def run_aging_alerts(dry_run: bool = False) -> dict[str, int]:
    """Check all open deals and send aging alerts.

    Returns:
        {"deals_checked": int, "alerts_sent": int, "throttled": int}
    """
    result: dict[str, int] = {
        "deals_checked": 0,
        "alerts_sent": 0,
        "throttled": 0,
    }

    # Warm pipeline cache
    try:
        if not _STAGE_NAME_CACHE:
            _refresh_pipeline_cache()
    except HubSpotClientError as exc:
        log.error("Could not refresh pipeline cache: %s", exc)
        return result

    now_ts = time.time()
    throttle = _load_throttle()
    throttle_updated = False

    for pipeline_id in (PIPELINE_F3E_RETAIL, PIPELINE_UFL_OSN_BDM):
        pipeline_name = _PIPELINE_NAME_CACHE.get(pipeline_id, pipeline_id)

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

            # Skip terminal stages
            stage_lower = stage_name.lower()
            if "closed" in stage_lower and ("won" in stage_lower or "lost" in stage_lower):
                continue

            result["deals_checked"] += 1

            age_days = _deal_age_days(props, now_ts)
            threshold = _get_threshold(stage_name)

            if age_days <= threshold:
                continue

            # Check throttle
            if _is_throttled(deal_id, throttle, now_ts):
                result["throttled"] += 1
                log.debug("Throttled deal %s (%s)", deal_id, deal_name)
                continue

            # Build alert text
            text = _build_alert_text(
                deal_name=deal_name,
                deal_id=deal_id,
                stage_name=stage_name,
                age_days=age_days,
                threshold=threshold,
                amount=amount,
                pipeline_name=pipeline_name,
            )

            # Resolve owner to Slack
            slack_id = _get_slack_id_for_owner(owner_id)
            if slack_id:
                sent = _send_slack_dm(slack_id, text, dry_run=dry_run)
            else:
                log.info(
                    "No Slack mapping for owner %s on deal %s -- using fallback channel",
                    owner_id, deal_id,
                )
                sent = _send_fallback_channel(text, dry_run=dry_run)

            if sent:
                result["alerts_sent"] += 1
                if not dry_run:
                    throttle[deal_id] = now_ts
                    throttle_updated = True

    if throttle_updated:
        _save_throttle(throttle)

    log.info(
        "Deal aging alerts: checked=%d alerts_sent=%d throttled=%d",
        result["deals_checked"],
        result["alerts_sent"],
        result["throttled"],
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
        help="Check deals and log; do not send Slack messages or update throttle state",
    )
    args = parser.parse_args()

    _setup_logging()
    log.info("=" * 60)
    log.info("Deal aging alerts starting (dry_run=%s)", args.dry_run)

    try:
        result = run_aging_alerts(dry_run=args.dry_run)
    except Exception as exc:
        log.exception("Deal aging alerts crashed: %s", exc)
        return 1

    log.info(
        "Done: deals_checked=%d alerts_sent=%d throttled=%d",
        result["deals_checked"],
        result["alerts_sent"],
        result["throttled"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
