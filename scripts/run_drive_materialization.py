#!/usr/bin/env python3
"""Nightly Drive materialization runner — distill the day's NEW swept KB chunks into
Drive _brain/swept/{ENTITY}/YYYY-MM-DD.md so a Drive-reading frontend (Tag) can answer
from swept knowledge, not just curated facts.

Reads already-embedded chunks from the LOCAL KB (no vector search, no rebuild, no
connector re-fetch). LEX is PHI-walled (LBHS excluded, GM-level scrubbed, dropped if
PHI survives the scrub). See src/cora/drive_materializer.py for the full contract.

Schedule: 5:45am AZ daily (after the kb-sync sweeps land, before gap-autofill 6:10 /
knowledge-review). Register with deployment/setup-drive-materialization-task.ps1.

Usage:
    python scripts/run_drive_materialization.py [--dry-run] [--lookback-hours N]
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora import drive_materializer  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = _REPO_ROOT / "logs"


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"drive-materialization-{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Materialize swept KB knowledge to Drive _brain/swept/")
    ap.add_argument("--dry-run", action="store_true",
                    help="Distill but write nothing and do not advance watermarks.")
    ap.add_argument("--lookback-hours", type=int, default=None,
                    help="Seed window for sources with no watermark yet (default 26h).")
    args = ap.parse_args(argv)

    _setup_logging()
    log = logging.getLogger("run_drive_materialization")
    log.info("starting drive materialization (dry_run=%s, lookback_hours=%s)",
             args.dry_run, args.lookback_hours)

    try:
        stats = drive_materializer.run(
            dry_run=args.dry_run,
            lookback_hours=args.lookback_hours,
        )
    except Exception as exc:  # noqa: BLE001 — a nightly task must exit cleanly, never crash-loop
        log.error("drive materialization failed: %s", exc, exc_info=True)
        return 1

    if stats.get("aborted"):
        log.warning("run aborted: %s (nothing written)", stats["aborted"])
        return 0

    log.info(
        "done: %d written, %d skipped, %d LEX-dropped, %d no-new-content; files=%d",
        stats["entities_written"], stats["entities_skipped"],
        stats["lex_dropped"], stats["entities_no_new"], len(stats["files"]),
    )
    for f in stats["files"]:
        log.info("  wrote %s", f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
