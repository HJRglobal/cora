#!/usr/bin/env python3
"""Email attachment auto-filer — scheduled entry point.

Scans monitored Gmail inboxes for emails with important attachments, classifies
each with Claude, and files them into the correct HJR-Founder-OS Drive folder
using a canonical naming convention.

Usage (called by Windows Task Scheduler — see deployment/setup-attachment-filer-task.ps1):
    uv run python scripts/run_attachment_filer.py

Options:
    --dry-run          Classify and log what would be filed; don't upload or write ledgers.
    --reconcile        Seed the content ledger from files already in Drive, then exit.
                       Run this ONCE after deploy (and after any manual de-dupe cleanup)
                       so the next live run dedups against documents already on Drive.
                       Read-only: never uploads, never creates folders.
    --lookback-hours N Override how many hours back to scan (default: env EMAIL_FILING_LOOKBACK_HOURS or 24).
    --accounts A,B     Comma-separated email list to override monitored-email-accounts.yaml.
    --with-kb          Index filed attachments into Cora's KB immediately (default: off;
                       the next incremental Drive sync will pick them up anyway).

Environment variables required (already in .env if Cora is running):
    GOOGLE_SERVICE_ACCOUNT_JSON   Path to service account JSON (same as calendar/drive)
    ANTHROPIC_API_KEY             For Claude haiku classification
    SLACK_BOT_TOKEN               For the post-run filing summary in Slack

Optional:
    EMAIL_FILING_NOTIFY_CHANNEL   Slack channel for summary (default: cora-filing)
    EMAIL_FILING_LOOKBACK_HOURS   Hours to look back on first run / if watermark missing (default: 24)
    CORA_DRIVE_ROOT_FOLDER_ID     Drive folder ID override to skip root lookup

See deployment/setup-attachment-filer-task.ps1 to register the scheduled task.
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

from cora.connectors.attachment_filer import (  # noqa: E402
    AttachmentFilerError,
    load_monitored_accounts,
    post_slack_summary,
    reconcile_ledger_from_drive,
    run_filer,
)

LOG_DIR = _REPO_ROOT / "logs"


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"attachment-filer-{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Classify only; don't upload or write ledgers")
    parser.add_argument("--reconcile", action="store_true", help="Seed content ledger from existing Drive files, then exit")
    parser.add_argument("--lookback-hours", type=int, default=None, help="Hours to scan if no watermark")
    parser.add_argument("--accounts", default=None, help="Comma-separated email overrides")
    parser.add_argument("--with-kb", action="store_true", help="Index filed attachments into KB immediately")
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("attachment-filer")
    log.info("=" * 60)
    log.info("Email attachment filer starting (dry_run=%s, reconcile=%s)", args.dry_run, args.reconcile)

    # Reconcile mode: seed the content ledger from existing Drive files and exit.
    if args.reconcile:
        try:
            stats = reconcile_ledger_from_drive()
        except Exception as exc:
            log.exception("Reconcile crashed: %s", exc)
            return 1
        log.info(
            "Reconcile done: scanned=%d seeded=%d ledger_size=%d",
            stats["scanned"], stats["seeded"], stats["ledger_size"],
        )
        return 0

    # Override lookback hours if specified
    if args.lookback_hours is not None:
        import os
        os.environ["EMAIL_FILING_LOOKBACK_HOURS"] = str(args.lookback_hours)

    # Resolve account list
    if args.accounts:
        accounts = [
            {"email": e.strip(), "name": e.strip().split("@")[0], "enabled": True}
            for e in args.accounts.split(",")
            if e.strip()
        ]
    else:
        try:
            accounts = load_monitored_accounts()
        except AttachmentFilerError as exc:
            log.error("Failed to load account list: %s", exc)
            return 1

    if not accounts:
        log.warning("No accounts to process")
        return 0

    # Optionally open KB for immediate indexing
    kb = None
    if args.with_kb:
        try:
            from cora.knowledge_base import KnowledgeBase
            kb_path = _REPO_ROOT / "data" / "cora_kb.db"
            kb = KnowledgeBase(kb_path)
            log.info("KB opened for immediate indexing: %s", kb_path)
        except Exception as exc:
            log.warning("Could not open KB — filing will continue without indexing: %s", exc)

    try:
        summaries = run_filer(accounts=accounts, dry_run=args.dry_run, kb=kb)
    except Exception as exc:
        log.exception("Filer crashed: %s", exc)
        return 1
    finally:
        if kb is not None:
            try:
                kb.close()
            except Exception:
                pass

    # Log per-account summaries
    total_filed = 0
    total_errors = 0
    for s in summaries:
        log.info(
            "%s: scanned=%d filed=%d skipped=%d errors=%d",
            s["email"], s["messages_scanned"], s["filed"], s["skipped"], s["errors"],
        )
        total_filed += s["filed"]
        total_errors += s["errors"]

    log.info("Total filed: %d | Total errors: %d", total_filed, total_errors)

    # Slack summary
    if not args.dry_run:
        post_slack_summary(summaries)

    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
