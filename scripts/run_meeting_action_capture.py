#!/usr/bin/env python3
"""Meeting Action Capture -- scheduled entry point.

Fetches new Fireflies transcripts, parses action items with Claude Haiku,
creates Asana tasks for each action item, and posts Slack digests to the
entity leadership channel.

Usage (called by Windows Task Scheduler -- see deployment/setup-meeting-action-capture-task.ps1):
    python scripts/run_meeting_action_capture.py

Options:
    --dry-run    Parse and log; don't create Asana tasks or post to Slack.

Environment variables required (already in .env if Cora is running):
    FIREFLIES_API_KEY      Fireflies GraphQL API key
    ANTHROPIC_API_KEY      For Claude Haiku action item parsing
    ASANA_PAT              For creating Asana tasks
    SLACK_BOT_TOKEN        For posting digests to Slack

See deployment/setup-meeting-action-capture-task.ps1 to register the scheduled task.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

from cora.connectors.fireflies_action_extractor import run_action_capture  # noqa: E402

LOG_DIR = _REPO_ROOT / "logs"


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
        help="Parse and log only; don't create Asana tasks or post to Slack",
    )
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("meeting-action-capture")
    log.info("=" * 60)
    log.info("Meeting action capture starting (dry_run=%s)", args.dry_run)

    try:
        result = run_action_capture(dry_run=args.dry_run)
    except Exception as exc:
        log.exception("Action capture crashed: %s", exc)
        return 1

    log.info(
        "Done: meetings_processed=%d tasks_created=%d errors=%d",
        result["meetings_processed"],
        result["tasks_created"],
        len(result["errors"]),
    )

    if result["errors"]:
        for err in result["errors"]:
            log.warning("Error: %s", err)

    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
