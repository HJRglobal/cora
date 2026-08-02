"""Weekly lexicon mining runner (Lexicon Flywheel S5).

Two lanes (see cora.lexicon_mining): lane A aggregates the resolver's own
telemetry; lane B is the swept-Slack corpus pass (friction-mining sibling).
Proposals ride Monday's 7am knowledge-review DM (D-011 -- Harrison-gated unless
graduated trust auto-writes an eligible Tier-0/1 item, audited + revertible).

DRY-RUN IS THE DEFAULT: without --write the run detects + prints a JSON summary
and writes NOTHING (no proposals, no fingerprint ledger, no candidates ledger).
With --write, proposals additionally require CORA_LEXICON=full; at 'resolve'
the run persists the candidates ledger only (the rollout brake).

Scheduled task: cowork-cora-lexicon-mining, weekly Sunday 17:50 AZ
(deployment/setup-lexicon-mining-task.ps1) -- registered with --write.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.lexicon_mining import MAX_PROPOSALS_PER_RUN, run_mining  # noqa: E402


def _setup_logging() -> logging.Logger:
    log_dir = _REPO_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"lexicon-mining-{date.today().isoformat()}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"),
                  logging.StreamHandler()],
    )
    return logging.getLogger("run_lexicon_mining")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="Persist ledgers + queue proposals (default: dry-run, "
                             "nothing written)")
    parser.add_argument("--lane", choices=("a", "b", "both"), default="both")
    parser.add_argument("--max-proposals", type=int, default=MAX_PROPOSALS_PER_RUN)
    args = parser.parse_args()

    log = _setup_logging()
    try:
        summary = run_mining(dry_run=not args.write, lane=args.lane,
                             max_proposals=args.max_proposals)
    except Exception as exc:  # noqa: BLE001
        log.error("lexicon mining failed: %s", exc, exc_info=True)
        return 1
    log.info("lexicon mining summary: %s", json.dumps(summary, ensure_ascii=False))
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
