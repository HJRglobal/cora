#!/usr/bin/env python3
"""Generate monthly deliverables for all active F3 fighters.

Run on the 1st of each month (or manually) to create:
  - 2 x IG Story  (due: last day of the month)
  - 1 x IG Post   (must tag @f3energy + #DrinkF3, due: last day of month)

Per fighter, per month. Skips fighters who already have deliverables for
the current month (idempotent — safe to re-run).

Posts a summary to #f3-athletes on completion.

Usage:
    python scripts/generate_monthly_deliverables.py             # current month
    python scripts/generate_monthly_deliverables.py --month 2026-07  # specific month
    python scripts/generate_monthly_deliverables.py --dry-run
"""

from __future__ import annotations

import argparse
import calendar
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(dotenv_path=_REPO_ROOT / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(
        open(sys.stdout.fileno(), "w", encoding="utf-8", errors="replace", closefd=False)
    )],
)
log = logging.getLogger("monthly-deliverables")

_F3_ATHLETES_CHANNEL = "C0B6GT3117Y"
_ALEX_SLACK_ID       = "U0B3VGWJTMJ"

# Required for all deliverables
_STORY_REQUIREMENTS  = "2 IG stories required: tag @f3energy + use #DrinkF3"
_POST_REQUIREMENTS   = "1 IG feed post required: tag @f3energy + use #DrinkF3"


def _month_last_day(year: int, month: int) -> str:
    last = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-{last:02d}"


def _already_has_deliverables(conn, athlete_name: str, campaign_month: str) -> bool:
    """Return True if this fighter already has deliverables for this month."""
    row = conn.execute(
        "SELECT COUNT(*) FROM influencer_deliverables "
        "WHERE athlete_name=? AND campaign_month=? AND status != 'waived'",
        (athlete_name, campaign_month),
    ).fetchone()
    return (row[0] if row else 0) > 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--month", default=None,
                        help="YYYY-MM to generate for (default: current month)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.month:
        try:
            year, month = [int(x) for x in args.month.split("-")]
        except Exception:
            log.error("--month must be YYYY-MM, got %r", args.month)
            return 1
    else:
        today = date.today()
        year, month = today.year, today.month

    month_label = f"{year}-{month:02d}"
    due_date    = _month_last_day(year, month)

    log.info("Generating deliverables for %s (due %s)", month_label, due_date)

    from cora.tools.influencer_client import _get_conn

    conn = _get_conn()
    try:
        fighters = conn.execute(
            "SELECT DISTINCT athlete_name, handle FROM influencer_handles "
            "WHERE platform='instagram' AND entity='F3E' ORDER BY athlete_name ASC"
        ).fetchall()
    finally:
        conn.close()

    if not fighters:
        log.warning("No fighters registered. Run scripts/seed_fighters.py first.")
        return 1

    log.info("Found %d registered fighters", len(fighters))

    created_count = 0
    skipped_count = 0

    for row in fighters:
        athlete_name = row[0]
        handle       = row[1]

        conn = _get_conn()
        try:
            if _already_has_deliverables(conn, athlete_name, month_label):
                log.info("  SKIP %s — already has deliverables for %s", athlete_name, month_label)
                skipped_count += 1
                continue

            if args.dry_run:
                log.info("  [DRY] Would create 3 deliverables for %s (@%s) due %s",
                         athlete_name, handle, due_date)
                created_count += 3
                continue

            import time as _t
            now = str(int(_t.time()))
            for dtype, reqs in [
                ("story", _STORY_REQUIREMENTS),
                ("story", _STORY_REQUIREMENTS),
                ("post",  _POST_REQUIREMENTS),
            ]:
                conn.execute(
                    """INSERT INTO influencer_deliverables
                       (athlete_name, platform, deliverable_type, due_date,
                        requirements, entity, campaign_month, status,
                        created_at, updated_at)
                       VALUES (?, 'instagram', ?, ?, ?, 'F3E', ?, 'pending', ?, ?)""",
                    (athlete_name, dtype, due_date, reqs, month_label, now, now),
                )
            conn.commit()
            log.info("  OK   %s (@%s) — 3 deliverables created", athlete_name, handle)
            created_count += 3

        finally:
            conn.close()

    log.info("Done: %d deliverables created, %d fighters skipped (already had %s)",
             created_count, skipped_count, month_label)

    if not args.dry_run and created_count > 0:
        _post_to_slack(month_label, due_date, created_count // 3, skipped_count)

    return 0


def _post_to_slack(month_label: str, due_date: str, fighter_count: int, skipped: int) -> None:
    """Post a brief confirmation to #f3-athletes."""
    import requests
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return
    text = (
        f":calendar: *{month_label} F3 Fighter Deliverables Generated*\n"
        f"{fighter_count} fighters x 3 deliverables = "
        f"{fighter_count * 3} total. All due by *{due_date}*.\n"
        f"Required: 2 IG stories + 1 IG post, tagging @f3energy + #DrinkF3.\n"
        f"_<@{_ALEX_SLACK_ID}> use `@Cora show fighter compliance` to see status anytime._"
    )
    try:
        requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"channel": _F3_ATHLETES_CHANNEL, "text": text, "mrkdwn": True},
            timeout=10,
        )
    except Exception as exc:
        log.warning("Slack post failed: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
