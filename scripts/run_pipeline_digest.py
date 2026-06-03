#!/usr/bin/env python3
"""Weekly Pipeline Digest -- DM Tommy with F3E pipeline + Alex with UFL pipeline.

Fires every Monday morning to give each owner a clean pipeline snapshot.

Tommy (U0B3RU5Q55G) receives:
  - Full F3E Retail pipeline summary
  - Count of deals exceeding stage aging thresholds

Alex (U0B3VGWJTMJ) receives:
  - UFL Sponsorship pipeline deal list (from default pipeline)
  - Stage breakdown and total value

Usage (Windows Task Scheduler):
    python scripts/run_pipeline_digest.py [--dry-run]

Environment variables required:
    HUBSPOT_PRIVATE_APP_TOKEN    HubSpot private app token
    SLACK_BOT_TOKEN              For sending DMs
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

from cora.tools.hubspot_client import (  # noqa: E402
    PIPELINE_F3E_RETAIL,
    PIPELINE_UFL_OSN_BDM,
    HubSpotClientError,
    _STAGE_NAME_CACHE,
    _refresh_pipeline_cache,
    get_deals_by_pipeline,
    get_f3e_pipeline_summary_text,
    _deal_url,
)

LOG_DIR = _REPO_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s -- %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            LOG_DIR / f"cora-{datetime.now().strftime('%Y-%m-%d')}.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("pipeline_digest")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_TOMMY_SLACK_ID = "U0B3RU5Q55G"
_ALEX_SLACK_ID = "U0B3VGWJTMJ"
_FALLBACK_CHANNEL = "C0B3K67J10T"  # #hjrg-leadership

# Per-stage aging thresholds (days) -- mirrors deal_aging_alerts.py
STAGE_THRESHOLDS: dict[str, int] = {
    "Identify":    14,
    "Outreach":    10,
    "Sample Sent": 7,
    "Qualified":   21,
    "Proposal":    14,
    "Negotiation": 7,
}
_DEFAULT_THRESHOLD = 21

# Keywords to identify UFL-related deals from default pipeline
_UFL_KEYWORDS = ["ufl", "united fight", "mma", "sponsorship", "fight league"]


def _az_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=-7)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_dm(slack_client, user_id: str) -> str | None:
    try:
        resp = slack_client.conversations_open(users=[user_id])
        return resp["channel"]["id"]
    except Exception as exc:
        log.warning("Failed to open DM with %s: %s", user_id, exc)
        return None


def _send_message(slack_client, user_id: str, text: str, dry_run: bool) -> bool:
    if dry_run:
        log.info("[DRY-RUN] DM to %s: %.120s", user_id, text)
        return True
    dm_ch = _open_dm(slack_client, user_id)
    if not dm_ch:
        try:
            slack_client.chat_postMessage(channel=_FALLBACK_CHANNEL, text=text)
            return True
        except Exception as exc:
            log.warning("Fallback channel post failed: %s", exc)
            return False
    try:
        slack_client.chat_postMessage(channel=dm_ch, text=text)
        return True
    except Exception as exc:
        log.warning("DM to %s failed: %s", user_id, exc)
        return False


def _deal_create_ts(deal: dict) -> float:
    """Return createdate as unix timestamp (0 if missing)."""
    props = deal.get("properties") or {}
    raw = props.get("createdate") or props.get("hs_lastmodifieddate") or ""
    if not raw:
        return 0.0
    try:
        # HubSpot returns ISO 8601 with milliseconds e.g. 2026-05-01T12:00:00.000Z
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def _days_in_stage(deal: dict) -> int:
    """Approximate days in current stage (using last modified date as proxy)."""
    ts = _deal_create_ts(deal)
    if ts == 0:
        return 0
    return int((time.time() - ts) / 86400)


def _stage_name(deal: dict) -> str:
    props = deal.get("properties") or {}
    stage_id = props.get("dealstage", "")
    return _STAGE_NAME_CACHE.get(stage_id, stage_id)


def _is_aging(deal: dict) -> bool:
    stage = _stage_name(deal)
    threshold = STAGE_THRESHOLDS.get(stage, _DEFAULT_THRESHOLD)
    return _days_in_stage(deal) > threshold


def _deal_amount(deal: dict) -> float:
    props = deal.get("properties") or {}
    try:
        return float(props.get("amount") or 0)
    except (ValueError, TypeError):
        return 0.0


def _deal_name(deal: dict) -> str:
    return (deal.get("properties") or {}).get("dealname") or "Unnamed deal"


# ---------------------------------------------------------------------------
# Tommy -- F3E Pipeline
# ---------------------------------------------------------------------------

def build_tommy_message(deals: list[dict], pipeline_text: str) -> str:
    aging_deals = [d for d in deals if _is_aging(d)]
    aging_count = len(aging_deals)

    msg = ":bar_chart: *Your F3E Pipeline -- Monday Update*\n\n"
    msg += pipeline_text.strip()
    if aging_count > 0:
        msg += f"\n\n:warning: *{aging_count} deal(s) need attention* (exceeding stage thresholds)."
    else:
        msg += "\n\n:white_check_mark: All deals are within stage thresholds."
    return msg


# ---------------------------------------------------------------------------
# Alex -- UFL Pipeline
# ---------------------------------------------------------------------------

def build_alex_message(deals: list[dict]) -> str:
    if not deals:
        return (
            ":bar_chart: *UFL Sponsorship Pipeline -- Monday Update*\n\n"
            "No active deals in the UFL/sponsorship pipeline right now."
        )

    total_value = sum(_deal_amount(d) for d in deals)
    stage_counts: dict[str, int] = {}
    for d in deals:
        stage = _stage_name(d) or "Unknown"
        stage_counts[stage] = stage_counts.get(stage, 0) + 1

    stage_lines = "\n".join(
        f"  - {stage}: {count} deal(s)" for stage, count in sorted(stage_counts.items())
    )
    deal_count = len(deals)

    msg = (
        f":bar_chart: *UFL Sponsorship Pipeline -- Monday Update*\n\n"
        f"*{deal_count} active deal(s)* | ${total_value:,.0f} total value\n\n"
        f"*By stage:*\n{stage_lines}\n\n"
    )

    # Top deals (up to 5) with links
    top_deals = sorted(deals, key=_deal_amount, reverse=True)[:5]
    deal_lines = []
    for d in top_deals:
        did = d.get("id") or ""
        name = _deal_name(d)
        amount = _deal_amount(d)
        url = _deal_url(did) if did else ""
        link = f"<{url}|{name}>" if url else name
        deal_lines.append(f"  - {link} (${amount:,.0f})")
    if deal_lines:
        msg += "*Top deals:*\n" + "\n".join(deal_lines)

    return msg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> dict[str, Any]:
    from slack_sdk import WebClient as SlackWebClient

    bot_token = os.getenv("SLACK_BOT_TOKEN")
    if not bot_token:
        log.error("SLACK_BOT_TOKEN not set")
        return {"error": "SLACK_BOT_TOKEN not set"}

    slack = SlackWebClient(token=bot_token)
    results: dict[str, Any] = {"tommy": False, "alex": False, "errors": []}

    # Warm pipeline cache
    try:
        _refresh_pipeline_cache()
    except Exception as exc:
        log.warning("Could not warm pipeline cache: %s", exc)

    # --- Tommy: F3E pipeline ---
    try:
        f3e_deals = get_deals_by_pipeline(PIPELINE_F3E_RETAIL)
        pipeline_text = get_f3e_pipeline_summary_text()
        tommy_msg = build_tommy_message(f3e_deals, pipeline_text)
        if _send_message(slack, _TOMMY_SLACK_ID, tommy_msg, dry_run):
            results["tommy"] = True
            log.info("Sent F3E digest to Tommy (%d deals)", len(f3e_deals))
    except HubSpotClientError as exc:
        err = f"HubSpot error for Tommy's F3E digest: {exc}"
        log.error(err)
        results["errors"].append(err)
        fallback = (
            ":bar_chart: *Your F3E Pipeline -- Monday Update*\n\n"
            ":warning: Could not retrieve pipeline data from HubSpot. Please check manually."
        )
        _send_message(slack, _TOMMY_SLACK_ID, fallback, dry_run)
    except Exception as exc:
        err = f"Unexpected error building Tommy's digest: {exc}"
        log.error(err)
        results["errors"].append(err)

    # --- Alex: UFL pipeline ---
    try:
        default_deals = get_deals_by_pipeline(PIPELINE_UFL_OSN_BDM)
        # Filter to UFL-related deals (or show all if UFL is paused -- show full default pipeline)
        ufl_deals = [
            d for d in default_deals
            if any(kw in _deal_name(d).lower() for kw in _UFL_KEYWORDS)
        ]
        # If no UFL-keyword deals found, show all default pipeline deals
        display_deals = ufl_deals if ufl_deals else default_deals
        alex_msg = build_alex_message(display_deals)
        if _send_message(slack, _ALEX_SLACK_ID, alex_msg, dry_run):
            results["alex"] = True
            log.info("Sent UFL digest to Alex (%d deals displayed)", len(display_deals))
    except HubSpotClientError as exc:
        err = f"HubSpot error for Alex's UFL digest: {exc}"
        log.error(err)
        results["errors"].append(err)
        fallback = (
            ":bar_chart: *UFL Sponsorship Pipeline -- Monday Update*\n\n"
            ":warning: Could not retrieve pipeline data from HubSpot. Please check manually."
        )
        _send_message(slack, _ALEX_SLACK_ID, fallback, dry_run)
    except Exception as exc:
        err = f"Unexpected error building Alex's digest: {exc}"
        log.error(err)
        results["errors"].append(err)

    log.info("Pipeline digest complete: %s", results)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cora weekly pipeline digest")
    parser.add_argument("--dry-run", action="store_true", help="Log without sending")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
    sys.exit(0)
