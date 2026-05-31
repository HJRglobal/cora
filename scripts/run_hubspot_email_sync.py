#!/usr/bin/env python3
"""Entry point for Gmail → HubSpot email sync.

Scans each user's Gmail inbox for emails involving known HubSpot contacts
and auto-logs them as HubSpot email engagements.

Usage:
    uv run python scripts/run_hubspot_email_sync.py [--dry-run]

Runs once and exits. Designed to be called hourly by Task Scheduler.
State is stored in data/hubspot-email-sync-state.json.

Exit codes: 0 = success, 1 = error
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("hubspot_email_sync")


def main() -> int:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot email sync")
    parser.add_argument("--dry-run", action="store_true", help="Log what would be done without writing to HubSpot")
    args = parser.parse_args()

    log.info("=== HubSpot email sync starting%s ===", " [DRY RUN]" if args.dry_run else "")

    try:
        from cora.connectors.hubspot_email_sync import run_sync
        run_sync(dry_run=args.dry_run)
    except Exception as exc:
        log.error("Sync failed: %s", exc, exc_info=True)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
