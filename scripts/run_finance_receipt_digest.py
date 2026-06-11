"""Weekly finance receipt digest — scheduled task `cowork-cora-finance-receipt-digest`.

Scans all monitored inboxes for newly-detected financial documents (receipts,
invoices, statements, order confirmations) since the last per-account
watermark, files their attachments into the "Receipts & Invoices Inbox"
Drive folder, and posts a digest to #hjr-finance. Dedup-ledgered so each
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

    result = finance_receipts.run_digest(
        dry_run=args.dry_run, lookback_days=args.lookback_days,
    )
    digest = finance_receipts.format_digest(
        result["rows"], result["accounts_scanned"],
    )

    log.info(
        "digest complete: %d new financial docs across %d accounts (%d errors)%s",
        len(result["rows"]), result["accounts_scanned"], result["errors"],
        " [dry-run]" if args.dry_run else "",
    )

    if args.dry_run or args.no_slack:
        print(digest)
    else:
        posted = finance_receipts.post_digest_to_slack(digest)
        if not posted:
            print(digest)
            return 1
    return 0 if result["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
