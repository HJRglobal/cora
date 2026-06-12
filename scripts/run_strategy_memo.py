#!/usr/bin/env python3
"""Weekly Sunday 18:30 AZ -- founder strategy memo (Org Synthesis Phase 4).

Gathers the cross-entity fact base (cash, pipeline, stalled decisions,
deadline radar, efficiency findings, KB momentum, health), snapshots it for
week-over-week deltas, synthesizes a strategy memo with Sonnet (FAIL-CLOSED:
API error = factual rollup with a "synthesis unavailable" note), DMs it to
Harrison ONLY, and files a copy to 00-Founder/_strategy-memos/ for the
nightly static_md KB ingest.

Standalone scheduled script -- does NOT import the bot process (app.py /
tool_dispatch / claude_client); no Cora restart is ever needed to ship
changes here.

Scheduled as: Cora - Strategy Memo   weekly Sunday 18:30 AZ
(one hour after Cora - Friction Mining, so the memo sees that run's
still-pending efficiency findings).

Exit codes:
    0 = success (memo produced; individual dead sources degrade to stubs)
    1 = fatal error
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

from cora.strategy_memo import run_memo  # noqa: E402

LOG_DIR = _REPO_ROOT / "logs"


def _setup_logging() -> None:
    # Windows consoles default to cp1252 -- live task/decision text carries
    # non-ASCII chars, so force UTF-8 (never crash the dry-run print).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / f"strategy-memo-{today}.log",
                                encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Gather + synthesize but write/send NOTHING "
                             "(no snapshot, no memo file, no DM)")
    parser.add_argument("--no-synth", action="store_true",
                        help="Skip the Sonnet call (forces the factual-rollup "
                             "fallback; useful for cheap plumbing tests)")
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("strategy-memo")
    log.info("=" * 60)
    log.info("Strategy memo run starting (dry_run=%s no_synth=%s)",
             args.dry_run, args.no_synth)

    try:
        kwargs = {}
        if args.no_synth:
            kwargs["synth_fn"] = lambda facts: None
        result = run_memo(dry_run=args.dry_run, **kwargs)
    except Exception as exc:  # noqa: BLE001
        log.error("Strategy memo run failed: %s", exc, exc_info=True)
        return 1

    log.info("Run complete: date=%s first_run=%s synthesized=%s delivered=%s "
             "memo_path=%s", result["date"], result["first_run"],
             result["synthesized"], result["delivered"],
             result["memo_path"] or "(none)")
    log.info("Section status: %s", result["sections_ok"])

    if args.dry_run:
        print("\n--- DRY RUN: FACT BASE ---\n")
        print(result["facts"])
        print("\n--- DRY RUN: MEMO THAT WOULD BE SENT ---\n")
        print(result["memo"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
