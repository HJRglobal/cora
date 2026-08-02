"""Daily revenue-ops cadence sweep (R1/R3).

REPORT-ONLY by default: advances ledger states from live Gmail thread reads
and writes a JSONL report line. NO DMs, no drafts, no cards -- this is the
B2 parallel-run posture until the 5-clean-day verifier passes.

--mode stage additionally stages nudges for nudge_due threads:
  - CORA_SEND_LIVE=tier1 + tier-1 playbook -> byte-exact stash + approve card
    DM to Harrison (nothing sends without his tap; ships dark).
  - otherwise -> Tier-0 threaded reply draft in the mailbox's Drafts.

--dry-run: no writes at all (prints the report only).

Usage:
  .venv\\Scripts\\python.exe scripts\\run_revops_sweep.py                # report-only
  .venv\\Scripts\\python.exe scripts\\run_revops_sweep.py --mode stage
  .venv\\Scripts\\python.exe scripts\\run_revops_sweep.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

import cora  # noqa: E402,F401  (installs the Slack egress sanitizer class patch)
from cora.revops import ledger, sweep  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("run_revops_sweep")

_LOG_DIR = _REPO_ROOT / "logs"


def _slack_client():
    import os

    from slack_sdk import WebClient

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    return WebClient(token=token) if token else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("report", "stage"), default="report")
    parser.add_argument("--dry-run", action="store_true", help="no writes at all")
    args = parser.parse_args()

    client = _slack_client() if args.mode == "stage" and not args.dry_run else None
    conn = ledger.connect()
    try:
        report = sweep.sweep(
            conn,
            mode=args.mode,
            slack_client=client,
            dry_run=args.dry_run,
        )
    finally:
        conn.close()

    print(json.dumps(report, indent=2, default=str))
    if not args.dry_run:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = _LOG_DIR / f"revops-sweep-{_dt.date.today().isoformat()}.log"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {"ts": _dt.datetime.now().isoformat(), **report}, default=str
                )
                + "\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
