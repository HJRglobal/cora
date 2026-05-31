#!/usr/bin/env python3
"""One-time bootstrap: join every existing public Slack channel.

Run this once after adding the channels:join scope to the Cora Slack app.
After this, new channels are joined automatically via the channel_created
event handler in app.py.

Usage:
    uv run python scripts/bootstrap_channel_membership.py
    uv run python scripts/bootstrap_channel_membership.py --dry-run

Exit codes: 0 = success, 1 = error
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bootstrap_channels")

# Channels to skip (noise channels not useful for synthesis)
_SKIP_NAMES = frozenset({
    "general", "random", "announcements",
})


def main() -> int:
    parser = argparse.ArgumentParser(description="Join all public Slack channels")
    parser.add_argument("--dry-run", action="store_true", help="List channels without joining")
    args = parser.parse_args()

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("SLACK_BOT_TOKEN not set")
        return 1

    client = WebClient(token=token)

    log.info("=== Channel Bootstrap%s ===", " [DRY RUN]" if args.dry_run else "")

    # Collect all public channels
    all_channels: list[dict] = []
    cursor = None
    while True:
        kwargs: dict = {"types": "public_channel", "limit": 200, "exclude_archived": True}
        if cursor:
            kwargs["cursor"] = cursor
        resp = client.conversations_list(**kwargs)
        all_channels.extend(resp.get("channels", []))
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.3)

    log.info("Found %d public channels total", len(all_channels))

    already_member = 0
    joined = 0
    skipped = 0
    errors = 0

    for ch in all_channels:
        name = ch.get("name", "")
        ch_id = ch["id"]

        if name in _SKIP_NAMES:
            log.info("  SKIP  #%s (excluded)", name)
            skipped += 1
            continue

        if ch.get("is_member"):
            log.info("  MEMBER  #%s", name)
            already_member += 1
            continue

        if args.dry_run:
            log.info("  [DRY] Would join  #%s", name)
            joined += 1
            continue

        try:
            client.conversations_join(channel=ch_id)
            log.info("  JOINED  #%s", name)
            joined += 1
            time.sleep(0.5)   # conservative rate limit for join calls
        except SlackApiError as exc:
            err_code = exc.response.get("error", "unknown")
            if err_code in ("already_in_channel", "method_not_supported_for_channel_type"):
                already_member += 1
            else:
                log.warning("  ERROR  #%s: %s", name, err_code)
                errors += 1

    log.info("")
    log.info("=== Bootstrap complete ===")
    log.info("  Already member : %d", already_member)
    log.info("  Joined         : %d", joined)
    log.info("  Skipped        : %d", skipped)
    log.info("  Errors         : %d", errors)

    if not args.dry_run:
        log.info("")
        log.info("Cora is now a member of all public channels.")
        log.info("New channels will be joined automatically via the channel_created event.")
        log.info("Run the nightly sweep with: uv run python scripts/run_channel_sweep.py")

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
