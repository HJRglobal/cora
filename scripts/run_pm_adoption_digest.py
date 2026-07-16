"""Weekly PM-adoption digest -- scheduled task `cowork-cora-pm-adoption-digest`.

Standalone, script-side (no bot restart -- activates at the next scheduled fire from the
working tree). All logic lives in src/cora/pm_metrics.py; this is a thin argparse +
logging + post wrapper.

This digest IS the Phase-2 go/no-go instrument: Cora-vs-UI created/completed, overdue
WoW trend, staleness, and per-person engagement. Review the trend over ~4-6 weeks.
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

from cora import pm_metrics  # noqa: E402

LOG_PATH = REPO_ROOT / "logs"


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly PM-adoption digest")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute + print; write no snapshot state, post nothing.")
    ap.add_argument("--no-slack", action="store_true",
                    help="Compute for real (writes the snapshot) but print instead of posting.")
    ap.add_argument("--also-channel", action="store_true",
                    help="Also post to #founder-operations (default: Harrison DM only).")
    ap.add_argument("--lookback-days", type=int, default=7)
    ap.add_argument("--stale-days", type=int, default=14)
    args = ap.parse_args()

    LOG_PATH.mkdir(parents=True, exist_ok=True)
    import datetime as _dt
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                LOG_PATH / f"pm-adoption-digest-{_dt.date.today().isoformat()}.log",
                encoding="utf-8"),
        ],
    )
    log = logging.getLogger("pm-adoption-digest")

    result = pm_metrics.run(
        lookback_days=args.lookback_days, stale_days=args.stale_days,
        write_state=not args.dry_run,
    )
    text = pm_metrics.format_digest(result)
    log.info(
        "pm-adoption digest built (cora_actions=%d asana=%s)%s",
        result["cora"]["total_this_week"],
        "ok" if result.get("asana") else "unavailable",
        " [dry-run]" if args.dry_run else "",
    )

    if args.dry_run or args.no_slack:
        print(text)
        return 0

    ok = pm_metrics.post_digest(text, also_channel=args.also_channel)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
