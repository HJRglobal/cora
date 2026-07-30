"""Weekly finance receipt digest — scheduled task `cowork-cora-finance-receipt-digest`.

Scans all monitored inboxes for newly-detected financial documents (receipts,
invoices, statements, order confirmations) since the last per-account
watermark, files their attachments into the "Receipts & Invoices Inbox"
Drive folder, and posts a digest to #founder-finance. Dedup-ledgered so each
receipt surfaces exactly once. See src/cora/finance_receipts.py.

Usage:
    .venv\\Scripts\\python.exe scripts\\run_finance_receipt_digest.py [--dry-run]
        [--lookback-days N] [--no-slack]

--dry-run: scan + classify, file nothing, advance no watermark, post nothing
           (prints the digest to stdout).
--no-slack: run for real (file + watermark) but print instead of posting.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from cora import finance_receipts  # noqa: E402

LOG_PATH = REPO_ROOT / "logs"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-slack", action="store_true")
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument(
        "--time-budget-min", type=float, default=45.0,
        help="Self-bound the filing scan to this many minutes so a slow run posts a "
             "PARTIAL digest instead of being killed by the task's 1h ExecutionTimeLimit "
             "(a backstop). 0 disables the budget.",
    )
    args = parser.parse_args()

    LOG_PATH.mkdir(parents=True, exist_ok=True)
    import datetime as _dt
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                LOG_PATH / f"finance-receipt-digest-{_dt.date.today().isoformat()}.log",
                encoding="utf-8",
            ),
        ],
    )
    log = logging.getLogger("finance-receipt-digest")

    budget = args.time_budget_min * 60 if args.time_budget_min and args.time_budget_min > 0 else None
    try:
        result = finance_receipts.run_digest(
            dry_run=args.dry_run, lookback_days=args.lookback_days,
            time_budget_seconds=budget,
        )
        digest = finance_receipts.format_digest(
            result["rows"], result["accounts_scanned"],
        )
    except Exception:
        # Slice 7: a crash here previously escaped to stderr as a bare exit 1 with NO
        # log-file trace (2026-07-27, log ended mid-upload), so the failure was
        # undiagnosable. Log the full traceback to the log file, raise a metadata-only
        # ops alert, and exit nonzero.
        log.exception("finance-receipt-digest crashed before the digest could be built")
        try:
            finance_receipts.alert_delivery_failure(0, 0)
        except Exception:  # noqa: BLE001
            log.error("finance-receipt-digest: crash alert also failed to send")
        return 1

    log.info(
        "digest complete: %d new financial docs across %d accounts (%d per-item error(s))%s%s",
        len(result["rows"]), result["accounts_scanned"], result.get("errors", 0),
        " [budget hit -- partial scan, rest next run]" if result.get("budget_hit") else "",
        " [dry-run]" if args.dry_run else "",
    )

    if args.dry_run or args.no_slack:
        print(digest)
        return 0

    posted = finance_receipts.post_digest_to_slack(digest)
    if not posted:
        # W4-02: never silently succeed-then-drop. The docs are filed but the summary
        # didn't reach the finance channel (e.g. it was archived). Loud metadata-only
        # alert + exit nonzero -- this is a GENUINE delivery failure worth flagging.
        log.error(
            "digest post FAILED -- raising delivery-failure alert "
            "(%d docs filed but summary not delivered)", len(result["rows"]),
        )
        finance_receipts.alert_delivery_failure(
            len(result["rows"]), result["accounts_scanned"],
        )
        print(digest)
        return 1

    # Slice 7: the digest posted -> success. Per-item errors (a transiently-unreachable
    # mailbox, a single un-parseable receipt) are EXPECTED and logged above; they must
    # NOT red the task. The old `return 2 if errors else 0` flipped LastResult nonzero
    # for 10+ days on any hiccup even though the digest posted fine -- alarm-blindness.
    return 0


if __name__ == "__main__":
    sys.exit(main())
