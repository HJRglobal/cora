#!/usr/bin/env python3
"""Slack Channel Health Monitor -- weekly report on dead/unmapped channels.

Fires weekly on Sunday at 04:00 UTC (9pm AZ Saturday).
Posts a digest to #hjrg-leadership.

Usage (Windows Task Scheduler):
    python scripts/run_channel_health_monitor.py [--dry-run]

Environment variables required:
    SLACK_BOT_TOKEN    For reading channel history and posting
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

from cora.connectors.slack_connector import list_joined_channels, SlackConnectorError  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("channel_health_monitor")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HJRG_LEADERSHIP_CHANNEL = "C0B3K67J10T"
ENTITY_CHANNELS_FILE = _REPO_ROOT / "data" / "maps" / "entity-channels.yaml"
DEAD_WINDOW_DAYS = 30
RATE_LIMIT_SLEEP = 0.3  # seconds between conversations_history calls


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_entity_channel_ids() -> set[str]:
    """Return all channel IDs mentioned in entity-channels.yaml."""
    try:
        with open(ENTITY_CHANNELS_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        ids: set[str] = set()
        for entity_data in (data.get("entities") or {}).values():
            for ch_id in entity_data.values():
                if isinstance(ch_id, str) and ch_id.startswith("C"):
                    ids.add(ch_id)
        return ids
    except Exception as exc:
        log.warning("Failed to load entity-channels.yaml: %s", exc)
        return set()


def _check_channel_activity(slack_client, channel_id: str, lookback_seconds: int) -> bool:
    """Return True if channel has at least 1 message in the lookback window."""
    try:
        oldest = time.time() - lookback_seconds
        resp = slack_client.conversations_history(
            channel=channel_id,
            limit=1,
            oldest=str(oldest),
        )
        messages = resp.get("messages", [])
        return len(messages) > 0
    except Exception as exc:
        log.warning("conversations_history failed for %s: %s", channel_id, exc)
        return True  # Assume active on error (don't flag as dead)


def build_report(
    checked: int,
    dead_channels: list[dict[str, Any]],
    missing_channels: list[dict[str, Any]],
) -> str:
    """Build the Slack message for the health report."""
    today = date.today().isoformat()
    lines = [f":health: *Channel Health Report -- {today}*", ""]

    if dead_channels:
        lines.append(f":zzz: *Dead channels (0 messages in {DEAD_WINDOW_DAYS}d):*")
        for ch in dead_channels:
            lines.append(f"  - #{ch['name']} ({ch['id']}) -- consider archiving")
        lines.append("")
    else:
        lines.append(f":zzz: *Dead channels:* none -- all channels active in {DEAD_WINDOW_DAYS}d")
        lines.append("")

    if missing_channels:
        lines.append(":question: *Channels Cora is in but NOT in entity-channels.yaml:*")
        for ch in missing_channels:
            lines.append(f"  - #{ch['name']} ({ch['id']}) -- add to entity-channels.yaml")
        lines.append("")
    else:
        lines.append(":question: *Unmapped channels:* none -- all channels are mapped")
        lines.append("")

    healthy = checked - len(dead_channels)
    lines.append(
        f":white_check_mark: {healthy} channels healthy | "
        f"{len(dead_channels)} dead | "
        f"{len(missing_channels)} unmapped"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> dict[str, int]:
    from slack_sdk import WebClient

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        log.error("SLACK_BOT_TOKEN not set")
        return {"channels_checked": 0, "dead": 0, "missing": 0}

    slack_client = WebClient(token=bot_token)
    entity_channel_ids = _load_entity_channel_ids()

    try:
        all_channels = list_joined_channels()
    except SlackConnectorError as exc:
        log.error("Failed to list channels: %s", exc)
        return {"channels_checked": 0, "dead": 0, "missing": 0}

    # Filter to non-DM, non-MPIM channels only
    channels = [
        ch for ch in all_channels
        if not ch.get("is_im") and not ch.get("is_mpim")
    ]

    dead_channels: list[dict[str, Any]] = []
    missing_channels: list[dict[str, Any]] = []
    lookback_sec = DEAD_WINDOW_DAYS * 86400

    for ch in channels:
        ch_id   = ch["id"]
        ch_name = ch.get("name", ch_id)

        # Dead check
        time.sleep(RATE_LIMIT_SLEEP)
        active = _check_channel_activity(slack_client, ch_id, lookback_sec)
        if not active:
            log.info("Dead channel: #%s (%s)", ch_name, ch_id)
            dead_channels.append({"id": ch_id, "name": ch_name})

        # Missing from entity map
        if ch_id not in entity_channel_ids:
            log.debug("Unmapped channel: #%s (%s)", ch_name, ch_id)
            missing_channels.append({"id": ch_id, "name": ch_name})

    channels_checked = len(channels)
    report = build_report(channels_checked, dead_channels, missing_channels)

    if dry_run:
        log.info("[DRY RUN] Would post to %s:\n%s", HJRG_LEADERSHIP_CHANNEL, report)
    else:
        try:
            slack_client.chat_postMessage(
                channel=HJRG_LEADERSHIP_CHANNEL,
                text=report,
            )
            log.info(
                "Channel health report posted: %d checked, %d dead, %d missing",
                channels_checked, len(dead_channels), len(missing_channels),
            )
        except Exception as exc:
            log.error("Failed to post report: %s", exc)

    return {
        "channels_checked": channels_checked,
        "dead": len(dead_channels),
        "missing": len(missing_channels),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Slack Channel Health Monitor")
    parser.add_argument("--dry-run", action="store_true", help="Log without posting")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    log.info("channel_health_monitor result: %s", result)
