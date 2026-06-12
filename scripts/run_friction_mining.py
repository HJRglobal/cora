#!/usr/bin/env python3
"""Weekly Sunday 17:30 AZ -- efficiency mining pass (Org Synthesis Phase 3).

Mines the swept KB corpus (slack/gmail/fireflies, last 14 days) plus Cora's
own question logs for process-friction signals (repeated questions, repeated
manual steps, stale handoffs, cross-entity duplication) and queues at most
5 proposals into the Harrison-gated 7am knowledge-review DM (D-011). Sunday
evening proposals ride Monday morning's knowledge-review run.

Standalone scheduled script -- does NOT import the bot process (app.py /
tool_dispatch); no Cora restart is ever needed to ship changes here.

Scheduled as: Cora - Friction Mining   weekly Sunday 17:30 AZ

Exit codes:
    0 = success
    1 = fatal error
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.friction_mining import (  # noqa: E402
    LOOKBACK_DAYS,
    MAX_PROPOSALS_PER_RUN,
    SIGNAL_CROSS_ENTITY_DUP,
    SIGNAL_MANUAL_STEPS,
    SIGNAL_REPEATED_QUESTION,
    SIGNAL_STALE_HANDOFF,
    run_mining,
)

LOG_DIR = Path(__file__).resolve().parents[1] / "logs"

_ALL_SIGNALS = {
    "repeated_question": SIGNAL_REPEATED_QUESTION,
    "repeated_manual_steps": SIGNAL_MANUAL_STEPS,
    "stale_handoff": SIGNAL_STALE_HANDOFF,
    "cross_entity_duplication": SIGNAL_CROSS_ENTITY_DUP,
}


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / f"friction-mining-{today}.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect + draft but write NOTHING (no proposals, no ledger)")
    parser.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--max-proposals", type=int, default=MAX_PROPOSALS_PER_RUN)
    parser.add_argument("--signals", default="",
                        help="Comma-separated subset of: " + ", ".join(_ALL_SIGNALS))
    args = parser.parse_args()

    _setup_logging()
    log = logging.getLogger("friction-mining")
    log.info("=" * 60)
    log.info("Friction mining run starting (dry_run=%s lookback=%dd cap=%d)",
             args.dry_run, args.lookback_days, args.max_proposals)

    signals = None
    if args.signals:
        requested = {s.strip() for s in args.signals.split(",") if s.strip()}
        unknown = requested - set(_ALL_SIGNALS)
        if unknown:
            log.error("Unknown signal(s): %s", ", ".join(sorted(unknown)))
            return 1
        signals = {_ALL_SIGNALS[s] for s in requested}

    try:
        summary = run_mining(
            lookback_days=args.lookback_days,
            max_proposals=args.max_proposals,
            signals=signals,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Friction mining run failed: %s", exc, exc_info=True)
        return 1

    log.info("Run complete: chunks=%d raw=%d deduped=%d drafted=%d proposed=%d",
             summary["chunks"], summary["raw_findings"], summary["after_dedup"],
             summary["drafted"], len(summary["proposed"]))
    for i, item in enumerate(summary["proposed"], 1):
        log.info("%s %d/%d [%s|%s] (%s) %s",
                 "[DRY RUN]" if args.dry_run else "[PROPOSED]",
                 i, len(summary["proposed"]),
                 item["signal_type"], item["confidence"], item["entity"], item["title"])
        log.info("    %s", item["recommendation"][:300])
        log.info("    route=%s | %s", item["route"], item["frequency"])
    if args.dry_run:
        print("\n--- DRY RUN SUMMARY (JSON) ---")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
