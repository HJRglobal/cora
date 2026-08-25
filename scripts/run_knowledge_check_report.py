"""Weekly knowledge-check participation report -> DM to Hannah (C11).

Spec of record: the 2026-08-11 addendum Harrison locked ("DMs Hannah directly
(not a channel post)", Monday morning, ahead of the MWF training-readiness
audit). Hannah owns training readiness; this is the surface she runs that audit
from.

WHY A SEPARATE SCRIPT rather than a --send flag on run_knowledge_check.py: that
script's --report branch returns before the roster gate, but its task action
would then point a Monday 07:20 trigger at the same script that performs
roster-wide ASKS -- an hour BEFORE the real 08:05 ask run. An arg typo or a
regression in that early-return would fire the whole day's 13 DMs early, and the
08:05 run would then skip everyone as already-handled. A separate script removes
that blast radius entirely and matches the one-script-per-digest pattern the rest
of the estate uses.

The aggregation is NOT rebuilt here. knowledge_check.participation_stats /
participation_report already fold the append-only event log
(data/state/knowledge-check-events.jsonl, 290 rows covering every pilot day) into
exactly the per-user asked / answered / confirmed / skipped / no-response
breakdown this report needs. The seed's premise -- that the 7-day answer TTL made
participation unmeasurable -- reasoned from the known-answers ARTIFACT; that TTL
only sweeps kc-entry blocks out of the answer files and never touches the event
log, which nothing prunes (compact_logs globs data/*.jsonl non-recursively, so
data/state/ never matches at any size).

Script-side: activates at its next fire from the working tree, no restart.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import knowledge_check as kc  # noqa: E402

_LOG_DIR = _REPO_ROOT / "logs"
try:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            _LOG_DIR / f"knowledge-check-report-{date.today().isoformat()}.log",
            encoding="utf-8"),
    ]
except Exception:  # noqa: BLE001 -- a log file must never stop the report
    _handlers = [logging.StreamHandler(sys.stdout)]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=_handlers)
log = logging.getLogger("knowledge-check-report")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    # DEFAULT 7, not 30. The runner's existing --report silently uses the
    # 30-day default, which is the wrong window for a weekly report and would
    # have made week-over-week movement invisible.
    ap.add_argument("--days", type=int, default=7,
                    help="Reporting window in days (default 7 -- weekly).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the report; send nothing.")
    ap.add_argument("--no-slack", action="store_true",
                    help="Alias for --dry-run.")
    args = ap.parse_args(argv)

    try:
        lines = kc.participation_report(days=args.days)
    except Exception as exc:  # noqa: BLE001
        log.error("could not build the participation report: %s", exc, exc_info=True)
        return 1

    # ALWAYS log it, including under the `off` kill switch -- a paused pilot
    # should still be visible in the log rather than going silent.
    for line in lines:
        log.info("%s", line)

    if args.dry_run or args.no_slack:
        log.info("[DRY-RUN] not sending.")
        return 0

    sent = kc.post_participation_report(lines)
    log.info("participation report %s (mode enabled=%s)",
             "DM'd to Hannah" if sent else "NOT sent", kc.enabled())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
