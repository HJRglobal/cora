"""Influencer scan — runs every 2 hours via Windows Task Scheduler.

Polls each F3 brand Instagram account for new tagged media and campaign hashtag posts.
Matches detections against the athlete handle registry and posts Slack alerts to the
influencer ops channel so Alex can confirm deliverables with a single @Cora command.

Usage (called by Task Scheduler — see deployment/setup-influencer-scan-task.ps1):
    uv run python scripts/run_influencer_scan.py

Environment variables required (add to .env):
    INSTAGRAM_F3E_USER_ID          Numeric IG Business Account ID for F3 Energy
    INSTAGRAM_F3E_ACCESS_TOKEN     Long-Lived User Access Token for F3 Energy
    INSTAGRAM_F3MOOD_USER_ID       (same for F3 Mood)
    INSTAGRAM_F3MOOD_ACCESS_TOKEN
    INSTAGRAM_F3PURE_USER_ID       (same for F3 Pure)
    INSTAGRAM_F3PURE_ACCESS_TOKEN
    SLACK_BOT_TOKEN                Cora's bot token (already set)
    INFLUENCER_SCAN_NOTIFY_CHANNEL Slack channel name without # (default: f3-sales)

See _shared/projects/cora/META_SETUP_GUIDE.md for instructions on getting
the IG User IDs and Access Tokens from Meta's developer portal.
"""

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

# Ensure the repo src/ is on the path when running directly with uv
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(_REPO_ROOT / ".env")

from cora.connectors import instagram_monitor
from cora.tools import influencer_client

log = logging.getLogger(__name__)

_BRAND_ACCOUNTS_PATH = _REPO_ROOT / "data" / "maps" / "brand-social-accounts.yaml"
_NOTIFY_CHANNEL = os.environ.get("INFLUENCER_SCAN_NOTIFY_CHANNEL", "f3-sales")


# ---------------------------------------------------------------------------
# Slack posting (direct Web API call — this script runs outside Cora's bot process)
# ---------------------------------------------------------------------------

def _post_to_slack(text: str) -> bool:
    """Post a message to the configured influencer channel via Slack Web API.

    Returns True on success. Logs and returns False on failure (non-fatal).
    """
    import requests  # local import so the module can be imported without requests installed

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("influencer_scan: SLACK_BOT_TOKEN not set — cannot post Slack notification")
        return False

    channel = _NOTIFY_CHANNEL.lstrip("#")
    from cora.slack_egress import sanitize_text  # noqa: PLC0415 -- B1: raw POST bypasses the WebClient patch
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"channel": channel, "text": sanitize_text(text), "mrkdwn": True},
        timeout=15,
    )
    data = resp.json() if resp.ok else {}
    if not data.get("ok"):
        log.warning(
            "influencer_scan: Slack post failed channel=%s error=%s",
            channel, data.get("error", resp.status_code),
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Main scan logic
# ---------------------------------------------------------------------------

def _load_brand_accounts() -> list[dict]:
    """Load brand account configs from brand-social-accounts.yaml."""
    if not _BRAND_ACCOUNTS_PATH.exists():
        log.error("influencer_scan: brand-social-accounts.yaml not found at %s", _BRAND_ACCOUNTS_PATH)
        return []
    with open(_BRAND_ACCOUNTS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("brands") or []


def _process_detections(
    detections: list[dict],
    brand_display_name: str,
) -> int:
    """Dedup, match athletes, post Slack notifications. Returns count of new detections."""
    new_count = 0
    for det in detections:
        post_id = det.get("post_id", "")
        platform = "instagram"
        if not post_id:
            continue

        # Skip already-processed posts
        if influencer_client.is_already_detected(platform, post_id):
            continue

        # Try to match the posting username to an athlete handle
        username = det.get("username", "")
        athlete_name: str | None = None
        if username:
            handle_row = influencer_client.get_athlete_by_handle(platform, username)
            if handle_row:
                athlete_name = handle_row["athlete_name"]

        # Log the detection
        influencer_client.log_detection(
            platform=platform,
            post_id=post_id,
            brand_handle=det.get("brand_handle", ""),
            athlete_name=athlete_name,
            athlete_handle=username or None,
            media_type=det.get("media_type"),
            post_url=det.get("permalink"),
            caption_snippet=(det.get("caption") or "")[:200],
            slack_notified=False,
        )

        # Format and post Slack notification
        slack_text = instagram_monitor.format_detection_for_slack(
            det,
            athlete_name=athlete_name,
            brand_display_name=brand_display_name,
        )
        posted = _post_to_slack(slack_text)
        if posted:
            influencer_client.mark_detection_notified(platform, post_id)
            log.info(
                "influencer_scan: notified Slack channel=%s post_id=%s athlete=%s",
                _NOTIFY_CHANNEL, post_id, athlete_name or "(unknown handle)",
            )

        new_count += 1

    return new_count


def run_scan() -> None:
    """Main entry point. Scans all configured brand accounts and posts Slack alerts."""
    brands = _load_brand_accounts()
    if not brands:
        log.warning("influencer_scan: no brand accounts configured — nothing to scan")
        return

    scan_time = datetime.now(tz=timezone.utc).isoformat()
    total_new = 0

    for brand in brands:
        display_name = brand.get("display_name", "F3 Brand")
        ig_config = brand.get("instagram")
        if not ig_config:
            continue

        handle = ig_config.get("handle", "")
        user_id = os.environ.get(ig_config.get("ig_user_id_env", ""), "")
        token = os.environ.get(ig_config.get("access_token_env", ""), "")

        if not user_id or not token:
            log.info(
                "influencer_scan: skipping %s — %s or %s not set in .env",
                display_name,
                ig_config.get("ig_user_id_env"),
                ig_config.get("access_token_env"),
            )
            continue

        # Check + refresh token if within 10 days of expiry
        token = instagram_monitor.check_and_refresh_token(
            token, ig_config.get("access_token_env", "")
        )

        # Load scan watermark (last-processed timestamp for this brand)
        since = instagram_monitor.get_watermark("instagram", handle)

        brand_new = 0

        # 1. Tagged media
        tagged = instagram_monitor.scan_tagged_media(
            ig_user_id=user_id,
            access_token=token,
            brand_handle=handle,
            since_timestamp=since,
        )
        brand_new += _process_detections(tagged, display_name)

        # 2. Hashtag scans
        for hashtag in ig_config.get("monitored_hashtags") or []:
            ht_results = instagram_monitor.scan_hashtag(
                ig_user_id=user_id,
                access_token=token,
                hashtag=hashtag,
                brand_handle=handle,
                since_timestamp=since,
            )
            brand_new += _process_detections(ht_results, display_name)

        # Update watermark to now (only move forward if scan succeeded)
        instagram_monitor.set_watermark("instagram", handle, scan_time)
        total_new += brand_new
        log.info(
            "influencer_scan: %s scan complete — %d new detections",
            display_name, brand_new,
        )

    log.info("influencer_scan: run complete — %d total new detections across all brands", total_new)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                _REPO_ROOT / "logs" / f"influencer-scan-{datetime.now().strftime('%Y-%m-%d')}.log",
                encoding="utf-8",
            ),
        ],
    )
    run_scan()
