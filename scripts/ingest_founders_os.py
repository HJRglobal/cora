"""Ingest the HJR-Founder-OS Google Drive folder into Cora's KB.

Sweeps the shared G:/My Drive/HJR-Founder-OS folder, classifying each file
by entity (from folder path) and relevance (Haiku score), then stores surviving
chunks in the same KB that all other Cora sources feed into.

Entity is determined from the top-level folder name — no guesswork:
  00-Founder          → FNDR
  01-HJR-Global       → HJRG
  02-F3-Energy        → F3E
  03-F3-Community     → F3C
  04-UFL              → UFL
  05-HJR-Productions  → HJRPROD
  06-HJR-Properties   → HJRP  (sub-entities: cinema-lanes, lci-realty, rogers-ranch)
  07-Big-D-Media      → BDM
  08-Lexington-Services → LEX (sub-entities: llc, lts, lbhs, lla — PHI guard active)
  09-One-Stop-Nutrition → OSN
  _shared             → FNDR

Phased backfill (run in order, verify each before proceeding):
  Phase 1  FNDR + HJRG   Foundation layer — CLAUDE.md, playbooks, templates
  Phase 2  F3E + OSN     Most active businesses
  Phase 3  HJRP+BDM+HJRPROD  Properties, media, productions
  Phase 4  LEX           Most sensitive — PHI guard, higher threshold
  Phase 5  F3C + UFL     Community, paused sports league

Usage:
  # Preview what would be ingested (no KB writes)
  python scripts/ingest_founders_os.py --dry-run

  # Preview specific entity
  python scripts/ingest_founders_os.py --entity F3E --dry-run

  # Run a phase
  python scripts/ingest_founders_os.py --phase 1

  # Run specific entity or comma-separated list
  python scripts/ingest_founders_os.py --entity F3E
  python scripts/ingest_founders_os.py --entity FNDR,HJRG

  # Full backfill (all entities, all files up to 10 years back)
  python scripts/ingest_founders_os.py --backfill

  # Nightly incremental — called by Task Scheduler
  python scripts/ingest_founders_os.py
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# ── Repo-root bootstrap ────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(_REPO_ROOT / ".env", override=True)

# ── Logging ────────────────────────────────────────────────────────────────────
# File handler added 2026-06-11: the scheduled task (cowork-cora-founders-os-sweep)
# had no stdout redirection and no file logging, so exit-code failures
# (SCHED_S_TASK_TERMINATED) were undiagnosable. Configured BEFORE the heavy
# imports below so an import failure still leaves a log line behind.
_LOG_DIR = _REPO_ROOT / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(
            _LOG_DIR / f"founders-os-sweep-{time.strftime('%Y-%m-%d')}.log",
            encoding="utf-8",
        ),
        logging.StreamHandler(
            open(sys.stdout.fileno(), "w", encoding="utf-8", errors="replace", closefd=False)
        ),
    ],
)
log = logging.getLogger("ingest-founders-os")

try:
    import anthropic

    from src.cora.connectors.drive_sweep import sweep_founders_os, FOUNDERS_OS_ROOT_ID
    from src.cora.knowledge_base.store import KnowledgeBase
except Exception:
    log.exception("Fatal: import failure before main()")
    sys.exit(1)

# ── Phase definitions ──────────────────────────────────────────────────────────
PHASES: dict[str, dict] = {
    "1": {
        "entities": "FNDR,HJRG",
        "label": "Foundation (FNDR + HJRG + _shared)",
    },
    "2": {
        "entities": "F3E,OSN",
        "label": "Active businesses (F3E + OSN)",
    },
    "3": {
        "entities": "HJRP,BDM,HJRPROD",
        "label": "Properties + Media (HJRP + BDM + HJRPROD)",
    },
    "4": {
        "entities": "LEX",
        "label": "Lexington Services (LEX -- PHI guard active, threshold=6)",
    },
    "5": {
        "entities": "F3C,UFL",
        "label": "Community + UFL (F3C + UFL)",
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest HJR-Founder-OS Drive folder into Cora KB"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview what would be ingested without writing to KB",
    )
    parser.add_argument(
        "--entity", type=str, default=None,
        help="Comma-separated entity codes to sweep (e.g. F3E or FNDR,HJRG). "
             "Omit to sweep all entities.",
    )
    parser.add_argument(
        "--phase", type=str, choices=list(PHASES.keys()), default=None,
        help="Run a predefined phase (1-5). Overrides --entity.",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="Ignore watermarks — process all files up to 10 years back.",
    )
    parser.add_argument(
        "--freshness-days", type=int, default=730,
        help="Days to look back on first run (default: 730 = 2 years). "
             "--backfill sets this to 3650.",
    )
    parser.add_argument(
        "--time-budget-min", type=int, default=105,
        help="Wall-clock self-budget in minutes (W4-01). The scheduled task's "
             "ExecutionTimeLimit is PT2H; a value < that lets the sweep stop "
             "CLEANLY and checkpoint instead of being SIGKILLed mid-commit. "
             "0 or negative = unlimited. --backfill defaults this to unlimited.",
    )
    args = parser.parse_args()

    # ── Resolve config ─────────────────────────────────────────────────────────
    sa_json_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json_path or not Path(sa_json_path).exists():
        log.error(
            "GOOGLE_SERVICE_ACCOUNT_JSON not set or file missing. "
            "Check .env — value should be an absolute path to the SA key JSON."
        )
        return 1

    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not anthropic_api_key:
        log.error("ANTHROPIC_API_KEY not set in .env")
        return 1

    # ── Resolve entity filter ──────────────────────────────────────────────────
    entity_filter: str | None = None
    if args.phase:
        phase_cfg = PHASES[args.phase]
        entity_filter = phase_cfg["entities"]
        log.info("Phase %s: %s", args.phase, phase_cfg["label"])
    elif args.entity:
        entity_filter = args.entity

    freshness_days = 3650 if args.backfill else args.freshness_days

    # A manual backfill runs at the console (no ExecutionTimeLimit), so it should
    # run to completion; only cut off the scheduled incremental sweep. If the
    # operator passes an explicit --time-budget-min they still win.
    time_budget_min: int | None = args.time_budget_min
    if args.backfill and args.time_budget_min == 105:  # untouched default
        time_budget_min = 0  # unlimited
    if time_budget_min is not None and time_budget_min <= 0:
        time_budget_min = None

    if args.dry_run:
        log.info("DRY RUN — no KB writes will occur")
    if args.backfill:
        log.info("BACKFILL mode — watermarks ignored, freshness_days=%d", freshness_days)

    # ── Run sweep ──────────────────────────────────────────────────────────────
    db_path = _REPO_ROOT / "data" / "cora_kb.db"
    kb = KnowledgeBase(db_path)
    client = anthropic.Anthropic(api_key=anthropic_api_key)

    start = time.time()
    result = sweep_founders_os(
        sa_json_path=sa_json_path,
        kb=kb,
        anthropic_client=client,
        root_folder_id=FOUNDERS_OS_ROOT_ID,
        entity_filter=entity_filter,
        freshness_days=freshness_days,
        dry_run=args.dry_run,
        time_budget_min=time_budget_min,
    )
    elapsed = time.time() - start

    if "error" in result:
        log.error("Sweep failed: %s", result["error"])
        return 1

    # ── Summary ────────────────────────────────────────────────────────────────
    mode = "DRY RUN" if args.dry_run else "LIVE"
    log.info(
        "[%s] Complete in %.1fs — entities=%d files_enumerated=%d "
        "extracted=%d chunks_ingested=%d phi_skipped=%d "
        "noise_filtered=%d dedup_skipped=%d deferred=%d%s",
        mode, elapsed,
        result.get("entities_swept", 0),
        result.get("files_enumerated", 0),
        result.get("files_extracted", 0),
        result.get("chunks_ingested", 0),
        result.get("phi_skipped", 0),
        result.get("noise_filtered", 0),
        result.get("dedup_skipped", 0),
        result.get("entities_deferred", 0),
        " [budget-interrupted — resumes next run]" if result.get("budget_interrupted") else "",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
