#!/usr/bin/env python3
"""Nightly Universal Session Capture run.

Harvests Claude Code session transcripts that closed without writing their own
capture note, distills each with Haiku, entity-tags it, and writes a session-
note into <FounderOS>/<entity>/_session-captures/YYYY-MM/. The nightly
static_md sync ingests them into Cora's KB; --with-kb also ingests immediately.

Scheduled as: cowork-cora-session-capture  Daily 5:00am AZ (AFTER the Slack /
Asana / Fireflies / static / drive syncs).
Register with: deployment\\setup-session-capture-task.ps1 (elevated PowerShell)

Usage:
    .venv\\Scripts\\python.exe scripts\\run_session_capture.py [--dry-run]
        [--lookback-hours N] [--max-sessions N] [--with-kb]

Exit codes: 0 = clean, 1 = fatal error
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)

sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import session_capture as scap  # noqa: E402

LOG_DIR = _REPO_ROOT / "logs"


def _setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"session-capture-{datetime.now().strftime('%Y-%m-%d')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("session-capture")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Universal Session Capture run.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would be captured; no files, KB writes, or ledger entries.")
    p.add_argument("--lookback-hours", type=int, default=24,
                   help="Only consider sessions active within this window (default 24).")
    p.add_argument("--max-sessions", type=int, default=50,
                   help="Cap sessions processed this run (default 50).")
    p.add_argument("--with-kb", action="store_true",
                   help="Also ingest each note into the KB immediately (idempotent).")
    p.add_argument("--no-cowork", action="store_true",
                   help="Skip the Cowork desktop store; harvest only ~/.claude/projects "
                        "Code sessions.")
    p.add_argument("--max-cowork-sessions", type=int, default=None,
                   help="Cap Cowork sessions processed this run (default = --max-sessions).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    log = _setup_logging()
    log.info("=" * 60)
    log.info("Session capture starting (dry_run=%s lookback=%dh cowork=%s)",
             args.dry_run, args.lookback_hours, not args.no_cowork)

    kb = None
    if args.with_kb and not args.dry_run:
        try:
            from cora.knowledge_base.store import KnowledgeBase
            kb = KnowledgeBase(_REPO_ROOT / "data" / "cora_kb.db")
        except Exception as exc:  # noqa: BLE001
            log.warning("KB unavailable (%s) — immediate ingest disabled", exc)

    results = scap.harvest(
        lookback_hours=args.lookback_hours,
        max_sessions=args.max_sessions,
        dry_run=args.dry_run,
        with_kb=args.with_kb,
        kb=kb,
        include_cowork=not args.no_cowork,
        max_cowork_sessions=args.max_cowork_sessions,
    )

    captured = [r for r in results if r.distilled and r.note_path]
    skipped = [r for r in results if not (r.distilled and r.note_path)]
    log.info("Run complete: %d captured, %d skipped, %d total examined",
             len(captured), len(skipped), len(results))
    for r in captured:
        log.info("  + %s  entity=%s phi=%s  %s",
                 r.session_id[:8], r.entity, r.phi, r.meta.get("topic", ""))
    for r in skipped:
        log.info("  - %s  skipped=%s", r.session_id[:8], r.skipped_reason)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        logging.getLogger("session-capture").error("Fatal error", exc_info=True)
        sys.exit(1)
