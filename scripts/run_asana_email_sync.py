#!/usr/bin/env python3
"""Gmail → Asana email sync entry point.

Scans each user's Gmail inbox for threads involving people referenced in
open Asana tasks, and posts matching threads as comments on those tasks.

Usage:
    python scripts/run_asana_email_sync.py [--dry-run]

Scheduled hourly via Task Scheduler as cowork-cora-asana-email-sync.
State stored in data/asana-email-sync-state.json.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(dotenv_path=_REPO_ROOT / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(
            _REPO_ROOT / "logs" /
            f"asana-email-sync-{__import__('datetime').date.today()}.log",
            encoding="utf-8",
        ),
        logging.StreamHandler(
            open(sys.stdout.fileno(), "w", encoding="utf-8",
                 errors="replace", closefd=False)
        ),
    ],
)
log = logging.getLogger("asana_email_sync")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Log what would happen without writing to Asana")
    args = parser.parse_args()

    log.info("=== Asana email sync starting%s ===",
             " [DRY RUN]" if args.dry_run else "")

    try:
        from cora.connectors.asana_email_sync import run_sync
        run_sync(dry_run=args.dry_run)
    except Exception as exc:
        log.error("Sync failed: %s", exc, exc_info=True)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
