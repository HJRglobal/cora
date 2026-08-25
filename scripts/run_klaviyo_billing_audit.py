#!/usr/bin/env python3
"""C14 (cq-118f8bbf842e): read-only Klaviyo billing & seat audit.

Derives the charge basis from segment profile counts, names never-engaged
segments as deactivation candidates, and reports the seat roster from canon.
Posts to #founder-operations.

READ-ONLY, AND STRUCTURALLY SO. `klaviyo_client` has exactly one request
primitive that passes the literal "GET" to httpx, and a test greps that source to
keep it that way. Ops Dept OS v1 makes BOTH contact-list deletion and billing
cleanup unauthorized, so this names candidates and stops -- no suppression, no
deletion, no recommendation to perform either.

DARK WITHOUT A CREDENTIAL, HONESTLY. There is no KLAVIYO_API_KEY in .env (the
same blocker cq-44645e3f79a3 recorded on 2026-08-18). With none, the profile
figures are reported UNAVAILABLE and the seat section -- which comes from the
roster, not the API -- still renders. It never reports zero.

Dry-run is the DEFAULT: prints the report, posts nothing. `--post` sends it.

Run:  python scripts/run_klaviyo_billing_audit.py [--post]
Suggested schedule: monthly, on a free minute outside 03:00-09:00 AZ.
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

from cora import klaviyo_audit  # noqa: E402
from cora.connectors import klaviyo_client as kc  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("klaviyo-audit")


def _post(text: str) -> bool:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("SLACK_BOT_TOKEN not set -- cannot post")
        return False
    try:
        from slack_sdk import WebClient  # noqa: PLC0415

        # B1 egress doctrine: route every outbound line through the boundary.
        from cora.reply_formatter import normalize_slack_bold  # noqa: PLC0415
        from cora.slack_egress import sanitize_text  # noqa: PLC0415

        WebClient(token=token).chat_postMessage(
            channel=klaviyo_audit.OPS_CHANNEL,
            text=normalize_slack_bold(sanitize_text(text)),
            unfurl_links=False,
            unfurl_media=False,
        )
        log.info("posted Klaviyo audit to #founder-operations")
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("post failed: %s", exc)
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--post", action="store_true",
                    help="post to #founder-operations (default: print only)")
    args = ap.parse_args(argv)

    if not kc.configured():
        log.warning("KLAVIYO_API_KEY not set -- the profile figures will report "
                    "UNAVAILABLE; the seat section still applies")

    account = kc.get_account()
    segments = kc.get_segments()
    audit = klaviyo_audit.build_audit(
        segments=segments,
        account=account,
        seat_holders=klaviyo_audit.seat_holders_from_roster(),
    )
    report = klaviyo_audit.format_report(audit)
    print(report)

    if not args.post:
        print("\n(dry run -- nothing posted; pass --post to send)")
        return 0
    return 0 if _post(report) else 1


if __name__ == "__main__":
    sys.exit(main())
