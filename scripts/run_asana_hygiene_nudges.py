#!/usr/bin/env python3
"""Asana Hygiene Nudges -- comment on overdue tasks to prompt action.

Fires daily at 06:30 UTC (after reconciliation at 05:30 UTC).
Loops all mapped users, finds tasks overdue >14 days, checks KB for recent
signal, and posts an Asana comment nudge on each stale task.

Usage (Windows Task Scheduler):
    python scripts/run_asana_hygiene_nudges.py [--dry-run]

Environment variables required:
    ASANA_PAT            Asana personal access token
    SLACK_BOT_TOKEN      (for sending DMs if needed -- not used in this script)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env")

from cora import nudge_ledger  # noqa: E402
from cora.tools.asana_client import (  # noqa: E402
    AsanaClientError,
    create_task_comment,
    get_user_tasks,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("asana_hygiene_nudges")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

THROTTLE_FILE = _REPO_ROOT / "data" / "state" / "hygiene_nudge_throttle.json"
USER_MAP_FILE = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
KB_DB_FILE    = _REPO_ROOT / "data" / "cora_kb.db"

THROTTLE_DAYS       = 7      # days between nudges on the same task (this system)
CROSS_SYSTEM_DAYS   = 14     # skip if EITHER nudge system touched the task within this window
OVERDUE_THRESHOLD   = 14     # days past due_on to qualify
MAX_PER_USER        = 5      # nudges per user per run
MAX_TOTAL           = 25     # nudges total per run
KB_LOOKBACK_DAYS    = 30     # days to look back in KB for signal

# Strings that indicate Visibility CPA tasks -- skip these
_VISIBILITY_SKIP_TERMS = frozenset([
    "visibility", "andrew stubbs", "sarah bertoglio",
    "hayden greber", "emily stubbs",
])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_throttle() -> dict[str, int]:
    if THROTTLE_FILE.exists():
        try:
            return json.loads(THROTTLE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_throttle(state: dict[str, int]) -> None:
    THROTTLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    THROTTLE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _load_users() -> list[dict[str, Any]]:
    with open(USER_MAP_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("users", [])


def _is_visibility_task(task_name: str) -> bool:
    lower = task_name.lower()
    return any(term in lower for term in _VISIBILITY_SKIP_TERMS)


def _is_lex_task(task: dict[str, Any]) -> bool:
    for membership in task.get("memberships", []):
        proj_name = (membership.get("project") or {}).get("name", "")
        if proj_name.upper().startswith("[LEX"):
            return True
    return False


def _has_kb_signal(task_name: str) -> bool:
    """Check the KB for recent activity mentioning this task name (last 30 days).

    Table is `knowledge_chunks` (the prior `chunks` name was wrong, so this guard
    silently never fired -- every run logged "no such table: chunks").

    Recency uses `date_modified` -- when the underlying source (Slack message,
    Asana task, Drive doc, Fireflies transcript) was last touched -- NOT
    `ingested_at`. After a full KB re-ingest every row's `ingested_at` is recent,
    which would make the window a no-op and degrade this to "name appears
    anywhere in the KB". `date_modified` reflects genuine recent activity.
    """
    if not KB_DB_FILE.exists():
        return False
    try:
        cutoff_ts = int(time.time()) - KB_LOOKBACK_DAYS * 86400
        with sqlite3.connect(str(KB_DB_FILE)) as conn:
            row = conn.execute(
                "SELECT 1 FROM knowledge_chunks "
                "WHERE content LIKE ? AND date_modified > ? LIMIT 1",
                (f"%{task_name[:60]}%", cutoff_ts),
            ).fetchone()
        return row is not None
    except Exception as exc:
        log.warning("KB signal check failed: %s", exc)
        return False


def _days_overdue(due_on: str) -> int:
    """Return number of days past due_on (positive = overdue)."""
    try:
        due = date.fromisoformat(due_on)
        return (date.today() - due).days
    except Exception:
        return 0


def _build_comment(first_name: str, days: int, task_gid: str) -> str:
    today_str = date.today().isoformat()
    return (
        f"Automated check-in: this task has been open {days} days past its due date. "
        f"Does it need an update, a new due date, or can it be closed? "
        f"(Cora automated nudge -- {today_str})"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> dict[str, int]:
    throttle = _load_throttle()
    users = _load_users()
    now_ts = int(time.time())
    run_id = f"hygiene-nudge-daily-{date.today().isoformat()}"

    tasks_checked = 0
    nudges_sent = 0
    skipped_throttle = 0
    skipped_signal = 0
    total_nudges = 0

    for user in users:
        asana_gid = user.get("asana_user_gid")
        if not asana_gid:
            continue

        display_name = user.get("display_name", "Team member")
        first_name = display_name.split()[0] if display_name else "Team member"

        try:
            tasks = get_user_tasks(str(asana_gid), max_tasks=50)
        except AsanaClientError as exc:
            log.warning("Failed to get tasks for %s: %s", display_name, exc)
            continue

        user_nudges = 0
        for task in tasks:
            if total_nudges >= MAX_TOTAL:
                break
            if user_nudges >= MAX_PER_USER:
                break

            task_gid = task.get("gid", "")
            task_name = task.get("name", "")
            due_on = task.get("due_on") or ""
            tasks_checked += 1

            if not due_on:
                continue

            overdue_days = _days_overdue(due_on)
            if overdue_days < OVERDUE_THRESHOLD:
                continue

            # Visibility CPA skip
            if _is_visibility_task(task_name):
                log.debug("Skipping Visibility CPA task: %s", task_name)
                continue

            # Cross-system lockout: skip if EITHER nudge system (this daily job
            # OR the weekly Cowork closure sweep) touched this task recently.
            # Reads the shared closure-nudges JSONL; enforces 1 comment/task/7d
            # via a stricter 14-day window. Fails open on read error.
            if nudge_ledger.recently_nudged(task_gid, within_days=CROSS_SYSTEM_DAYS):
                log.debug("Skipping task nudged by another system <%dd ago: %s",
                          CROSS_SYSTEM_DAYS, task_name)
                skipped_throttle += 1
                continue

            # Local throttle check (secondary guard)
            last_nudged = throttle.get(task_gid, 0)
            if now_ts - last_nudged < THROTTLE_DAYS * 86400:
                skipped_throttle += 1
                continue

            # KB signal check
            if _has_kb_signal(task_name):
                log.debug("Skipping task with recent KB signal: %s", task_name)
                skipped_signal += 1
                continue

            comment = _build_comment(first_name, overdue_days, task_gid)

            if not dry_run:
                try:
                    create_task_comment(task_gid, comment)
                    log.info(
                        "Nudge sent: task_gid=%s user=%s overdue=%dd",
                        task_gid, display_name, overdue_days,
                    )
                    # Record in the shared ledger so the weekly closure sweep
                    # respects this fire (and vice versa).
                    nudge_ledger.record_nudge(
                        task_gid,
                        task_name=task_name,
                        assignee_user=display_name,
                        assignee_gid=str(asana_gid),
                        signal_source="task_staleness_daily",
                        signal_summary=f"{overdue_days}d overdue; no recent KB signal.",
                        run_id=run_id,
                    )
                except AsanaClientError as exc:
                    log.warning("Failed to comment on task %s: %s", task_gid, exc)
                    continue
            else:
                log.info(
                    "[DRY RUN] Would nudge: task_gid=%s user=%s overdue=%dd comment=%.80s",
                    task_gid, display_name, overdue_days, comment,
                )

            throttle[task_gid] = now_ts
            nudges_sent += 1
            user_nudges += 1
            total_nudges += 1

    if not dry_run and nudges_sent > 0:
        _save_throttle(throttle)

    return {
        "tasks_checked": tasks_checked,
        "nudges_sent": nudges_sent,
        "skipped_throttle": skipped_throttle,
        "skipped_signal": skipped_signal,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Asana Hygiene Nudges")
    parser.add_argument("--dry-run", action="store_true", help="Log without posting comments")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    log.info("asana_hygiene_nudges result: %s", result)
