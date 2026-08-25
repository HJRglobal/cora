#!/usr/bin/env python3
"""C13 (cq-015b3bc779e9): monthly "are the expected invoices filed?" check.

Reads data/maps/finance-expected-invoices.yaml, checks the attachment filer's own
content ledger for a matching filing in the last CLOSED month, and posts one
short report to #hjrg-finance. Read-only: two file reads and one Slack post.

WHY THIS EXISTS RATHER THAN A RETRIEVAL LANE. The seed asked for a Google Ads
invoice retrieval lane. Verify-first found the retrieval lane already works -- the
filer has been filing Google WORKSPACE invoices monthly on its own -- and that
Google ADS invoices have never arrived at any monitored mailbox at all (zero in
the ledger ever; zero billing emails in 120 days). No retrieval code can fetch a
document that was never delivered. What was actually missing is that nobody is
TOLD, so it stays missing quietly, month after month. This says it out loud until
the delivery is fixed.

Dry-run is the DEFAULT: prints the report, posts nothing. `--post` sends it.

Run:  python scripts/run_expected_invoice_check.py [--post] [--period YYYY-MM]
Suggested schedule: monthly, a few days into the month (after the filer has had
time to see the period's invoices), on a free minute outside 03:00-09:00 AZ.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import expected_invoices  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("expected-invoices")

#: #hjrg-finance -- the same target as the close pack and the adherence check.
#: #hjr-finance (C0BAK65N4TA) has been archived since 2026-08-04.
HJRG_FINANCE_CHANNEL = "C0B3V5SDNAG"


def _post(text: str) -> bool:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("SLACK_BOT_TOKEN not set -- cannot post")
        return False
    try:
        from slack_sdk import WebClient  # noqa: PLC0415

        # B1 egress doctrine: a WebClient sender in a script that imports no cora
        # module bypasses the class-level patch, so route through the boundary
        # explicitly. (This script DOES import cora, but the CI guard enforces
        # both halves and the explicit call is the reviewable one.)
        from cora.reply_formatter import normalize_slack_bold  # noqa: PLC0415
        from cora.slack_egress import sanitize_text  # noqa: PLC0415

        WebClient(token=token).chat_postMessage(
            channel=HJRG_FINANCE_CHANNEL,
            text=normalize_slack_bold(sanitize_text(text)),
            unfurl_links=False,
            unfurl_media=False,
        )
        log.info("posted expected-invoice report to #hjrg-finance")
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("post failed: %s", exc)
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--post", action="store_true",
                    help="post to #hjrg-finance (default: print only)")
    ap.add_argument("--period", default="",
                    help="YYYY-MM to check (default: the last closed month)")
    args = ap.parse_args(argv)

    result = expected_invoices.assess(args.period or None)
    report = expected_invoices.format_report(result)
    print(report)
    flags = expected_invoices.flag_count(result)
    print(f"\n[{flags} row(s) need a human]")

    if not args.post:
        print("\n(dry run -- nothing posted; pass --post to send)")
        return 0
    return 0 if _post(report) else 1


if __name__ == "__main__":
    sys.exit(main())
