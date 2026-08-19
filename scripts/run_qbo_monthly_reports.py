"""Populate the accounting monthly-report folder from QBO (cq-96adf03bcda3).

Replaces Justin's manual QBO export loop (decision F5, 2026-08-18): for each
provisioned QBO realm, pull the prior month's P&L + Balance Sheet and write
naming-convention .xlsx files into
``01-HJR-Global/accounting/monthly-reports/{filing-month}/`` on ``G:``.

    # what WOULD be written for the last completed month (no writes)
    .venv\\Scripts\\python.exe scripts\\run_qbo_monthly_reports.py

    # actually write
    .venv\\Scripts\\python.exe scripts\\run_qbo_monthly_reports.py --apply

    # backfill a specific REPORT month (folder is derived: 2026-05 -> 2026-06/)
    .venv\\Scripts\\python.exe scripts\\run_qbo_monthly_reports.py --month 2026-05 --apply

DRY-RUN IS THE DEFAULT, on purpose. A Code session stages file-writing work and
Harrison runs it; the scheduled task passes ``--apply`` explicitly. That also
means a mis-registered task fails safe (it reports instead of writing).

NEVER OVERWRITES. A same-named file already in the folder (Justin or Hayden filed
it by hand) is left untouched; Cora writes a ``-cora`` sibling and the summary
reports whether the two agree on a headline line. The manual upload therefore
becomes an optional parity cross-check, never something this job can clobber.

NO LLM (D-095). Every value is copied verbatim from the QBO report JSON.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import qbo_monthly_reports as qmr  # noqa: E402

log = logging.getLogger("qbo-monthly-reports")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--month",
        help="REPORT month as YYYY-MM (the month the statements cover). "
             "Defaults to the last completed month. The filing folder is always "
             "derived as this month + 1.",
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="Actually write the files. Omitted = dry-run report only.",
    )
    ap.add_argument(
        "--entity", action="append", default=None, metavar="CODE",
        help="Limit to these realm codes (repeatable). Default: all provisioned.",
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)

    try:
        report_month = args.month.strip() if args.month else qmr.previous_month()
        qmr.month_bounds(report_month)  # validate early, fail loudly
    except Exception as exc:  # noqa: BLE001
        log.error("bad --month: %s", exc)
        return 2

    sources = qmr.Sources()
    if args.entity:
        wanted = {e.strip().upper() for e in args.entity if e.strip()}
        all_provisioned = qmr.Sources().provisioned
        sources = qmr.Sources(
            provisioned=lambda: [e for e in all_provisioned() if e.upper() in wanted])

    summary = qmr.build_month(report_month, sources=sources, apply=args.apply)
    print(qmr.format_summary(summary))

    if summary.get("error"):
        return 1
    # A run that wrote nothing at all is a problem worth a non-zero exit so the
    # nightly health check can see it; a run with SOME output is a success with
    # reported gaps (a single dead realm must not fail the whole month).
    return 0 if summary.get("written") else 1


if __name__ == "__main__":
    raise SystemExit(main())
