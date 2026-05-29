#!/usr/bin/env python3
"""Drive fact extractor — daily 4:00am AZ scheduled entry point.

Runs after the nightly drive_sweep (3:30am) has finished ingesting Drive content
into the KB. Reads recently-ingested drive_sweep chunks, uses Claude Haiku to
extract structured facts (people, companies, deals, projects, decisions, amounts),
stores them in drive_extracted_facts, and optionally converts high-confidence facts
into Harrison-gated knowledge proposals via knowledge_review.propose_update().

Build 2 (extraction) and Build 3 (proposals) are wired here.

Usage (called by Windows Task Scheduler — see deployment/setup-drive-extractor-task.ps1):
    .venv\\Scripts\\python.exe scripts\\run_drive_extractor.py --propose

Options:
    --dry-run          Extract and log but do NOT write to DB or propose updates.
    --backfill         Ignore extraction watermark — re-extract all chunks in window.
    --lookback-days N  Lookback window for chunk query (default: 7).
    --propose          After extraction, run the proposal loop (Build 3).
    --propose-only     Skip extraction; run proposal loop only against existing facts.

Exit codes:
    0 = success
    1 = fatal error
    2 = partial (some operations failed)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

LOG_DIR  = _REPO_ROOT / "logs"
DB_PATH  = _REPO_ROOT / "data" / "cora_kb.db"


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"drive-extractor-{today}.log"
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
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Extract and log; don't write to DB or propose")
    parser.add_argument("--backfill", action="store_true",
                        help="Ignore watermark — re-extract all chunks in window")
    parser.add_argument("--lookback-days", type=int, default=7,
                        help="Days back to look for drive_sweep chunks (default: 7)")
    parser.add_argument("--propose", action="store_true",
                        help="After extraction, run proposal loop (Build 3)")
    parser.add_argument("--propose-only", action="store_true",
                        help="Skip extraction; run proposal loop only")
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("run_drive_extractor")
    log.info("=" * 60)
    log.info(
        "Drive extractor starting (dry_run=%s, backfill=%s, lookback_days=%d, "
        "propose=%s, propose_only=%s)",
        args.dry_run, args.backfill, args.lookback_days,
        args.propose, args.propose_only,
    )

    # Validate environment
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key and not args.propose_only:
        log.error("ANTHROPIC_API_KEY not set — cannot run extraction")
        return 1

    if not DB_PATH.exists():
        log.error("KB DB not found at %s", DB_PATH)
        return 1

    exit_code = 0

    # ─── Phase 1: Extraction (Build 2) ────────────────────────────────────────
    if not args.propose_only:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
        except Exception as exc:
            log.error("Could not build Anthropic client: %s", exc)
            return 1

        try:
            from cora.connectors.drive_extractor import run_extraction
            stats = run_extraction(
                client,
                db_path=DB_PATH,
                lookback_days=args.lookback_days,
                backfill=args.backfill,
                dry_run=args.dry_run,
            )
            log.info(
                "Extraction done: files=%d extracted=%d stored=%d errors=%d",
                stats["files_processed"], stats["facts_extracted"],
                stats["facts_stored"], stats["errors"],
            )
            if stats["errors"] > 0:
                exit_code = 2
        except Exception as exc:
            log.exception("Extraction crashed: %s", exc)
            return 1

    # ─── Phase 2: Proposals (Build 3) ─────────────────────────────────────────
    if args.propose or args.propose_only:
        try:
            from cora.connectors.drive_extractor import run_proposal_loop
            pstats = run_proposal_loop(db_path=DB_PATH, dry_run=args.dry_run)
            log.info(
                "Proposals done: proposed=%d skipped=%d errors=%d",
                pstats["proposed"], pstats["skipped"], pstats["errors"],
            )
            if pstats["errors"] > 0:
                exit_code = 2
        except Exception as exc:
            log.exception("Proposal loop crashed: %s", exc)
            return 1

    log.info("Drive extractor DONE (exit=%d)", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
