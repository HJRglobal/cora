#!/usr/bin/env python3
"""Monday 08:15 AZ -- finance SOP adherence check (A1-A3), deterministic facts only.

Runs the three SOP-adherence checks and writes a dated facts block:

  A1  cash-sheet freshness   -- the REAL live Standing ACTUALS sheet, <=7d old
  A2  Clover export          -- RETIRED (one static lane_retired fact; no alarms)
  A3  monthly filing presence + per-entity bank-statement freshness

OUTPUTS
  data/state/finance-adherence-facts.json                     (close pack reads this)
  G:\\...\\01-HJR-Global\\accounting\\finance-adherence-facts.md  (weekly review task
                                                               reads this; in place)

WHY 08:15 MONDAY: the facts block must exist before its two consumers run -- the
close-support pack at 09:00 and the Cowork weekly finance review at 13:00. Both
therefore read facts computed the same morning.

NO MODEL CALL. This job is entirely deterministic file reads, so no llm_usage
`caller=` tag applies -- see the module docstring; that is a property of a model-free
job, not the un-tagged-spend omission the 2026-08-02 audit found elsewhere.

READ-ONLY against every finance source. The only writes are this job's own two
facts artifacts. The TIER_3 finance firewall is untouched: the optional Slack
summary line carries no dollar figure by construction and goes only to
#hjrg-finance.

Usage:
    .venv\\Scripts\\python.exe scripts\\run_finance_adherence_check.py --dry-run
    .venv\\Scripts\\python.exe scripts\\run_finance_adherence_check.py
    .venv\\Scripts\\python.exe scripts\\run_finance_adherence_check.py --post-summary

Registered as Windows Task Scheduler task: cowork-cora-finance-adherence
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

# Windows consoles default to cp1252; the facts block renders characters it cannot
# encode. Harden stdout so --dry-run (the pre-flight gate) can never die on output.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            _REPO_ROOT / "logs"
            / f"finance-adherence-{datetime.datetime.now().strftime('%Y-%m-%d')}.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("finance-adherence")

# #hjrg-finance -- the ONLY channel this job may post to. Same target as the close
# pack; #hjr-finance (C0BAK65N4TA) is archived as of 2026-08-04.
HJRG_FINANCE_CHANNEL = "C0B3V5SDNAG"


def _post_summary(line: str) -> bool:
    """Post the one-line summary to #hjrg-finance. Never raises."""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("adherence: SLACK_BOT_TOKEN not set -- cannot post summary")
        return False
    try:
        from slack_sdk import WebClient  # noqa: PLC0415

        from cora.reply_formatter import normalize_slack_bold  # noqa: PLC0415
        from cora.slack_egress import sanitize_text  # noqa: PLC0415

        WebClient(token=token).chat_postMessage(
            channel=HJRG_FINANCE_CHANNEL,
            text=normalize_slack_bold(sanitize_text(line)),
            unfurl_links=False,
            unfurl_media=False,
        )
        log.info("adherence: summary posted to #hjrg-finance")
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("adherence: summary post failed: %s", exc)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the facts block; write nothing and post nothing")
    parser.add_argument("--post-summary", action="store_true",
                        help="Also post the one-line summary to #hjrg-finance")
    args = parser.parse_args()

    from cora import finance_adherence  # noqa: PLC0415

    log.info("=== finance adherence check starting (dry_run=%s) ===", args.dry_run)
    report = finance_adherence.build_report()

    for fact in report.facts:
        log.info("  [%s] %s", fact.status, fact.line())
    log.info(
        "adherence: %d check(s), %d problem(s), %d unreadable",
        len(report.facts), len(report.problems), len(report.unknowns),
    )

    if args.dry_run:
        print("\n" + "=" * 72)
        print("[DRY RUN] facts block (markdown)")
        print("=" * 72)
        print(report.to_markdown())
        print("=" * 72)
        print("[DRY RUN] Slack summary line:")
        print("  " + report.summary_line())
        print(f"[DRY RUN] would write: {finance_adherence.FACTS_JSON_PATH}")
        print(f"[DRY RUN] would write: {finance_adherence.FACTS_MD_PATH}")
        return 0

    json_path = finance_adherence.write_facts_json(report)
    log.info("adherence: wrote %s", json_path)

    md_path = finance_adherence.write_facts_markdown(report)
    if md_path:
        log.info("adherence: wrote %s", md_path)
    else:
        # Non-fatal: the local JSON is authoritative for the close pack. Exit code
        # stays 0 so a Drive blip does not read as a failed adherence check.
        log.warning("adherence: Drive-side facts block NOT written (mount unavailable)")

    if args.post_summary:
        _post_summary(report.summary_line())

    log.info("=== finance adherence check complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
