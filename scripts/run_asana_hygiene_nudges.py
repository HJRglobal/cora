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
import re
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
from cora.nudge_ledger import closed_task_guard  # noqa: E402
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
DEFERRED_FILE = _REPO_ROOT / "data" / "state" / "hygiene-deferred.jsonl"
USER_MAP_FILE = _REPO_ROOT / "data" / "maps" / "slack-to-asana.yaml"
KB_DB_FILE    = _REPO_ROOT / "data" / "cora_kb.db"

THROTTLE_DAYS       = 7      # days between nudges on the same task (this system)
CROSS_SYSTEM_DAYS   = 14     # skip if EITHER nudge system touched the task within this window
OVERDUE_THRESHOLD   = 14     # days past due_on to qualify
MAX_PER_USER        = 5      # Tier-1 nudges per user per run
MAX_TOTAL           = 25     # Tier-1 nudges total per run
MAX_TIER0           = 15     # Tier-0 (critical) nudges per run — bypass the Tier-1 caps
MAX_TIER0_PER_USER  = 5      # Tier-0 nudges per user per run (so one user can't take the whole Tier-0 budget)
KB_LOOKBACK_DAYS    = 30     # days to look back in KB for signal

# Tier-0 = compliance / LEX-revalidation / P0 / urgent. These nudges bypass the
# Tier-1 volume caps (bounded by MAX_TIER0 + MAX_TIER0_PER_USER) so a critical
# overdue task is never starved on a high-volume day (WS10). Patterns are
# HIGH-SIGNAL only — bare "audit"/"deadline" were DROPPED (D-051 review: they
# escalated routine HJR task titles like "Drive cleanup audit" / "BCB ingredient
# deadline", letting one user bypass MAX_PER_USER). A compliance audit still
# matches via "compliance"; a real revalidation via "revalidat".
_TIER0_RE = re.compile(
    r"🚨|\bp0\b|\burgent\b|\bcompliance\b|revalidat|lex[- ]reval",
    re.IGNORECASE,
)

# Strings that indicate Visibility CPA tasks -- skip these
_VISIBILITY_SKIP_TERMS = frozenset([
    "visibility", "andrew stubbs", "sarah bertoglio",
    "hayden greber", "emily stubbs",
])

# Asana auto-generated system reminders that are NOT real work -- skip these.
# The canonical term set + matcher now live in cora.asana_filters (WS12) so the
# nudge lane, reconciliation, the brief, and the plate share ONE definition and
# cannot drift. get_user_tasks also filters these at the source.
from cora.asana_filters import is_system_noise_task as _is_system_noise_task  # noqa: E402


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


def _importance_tier(task_name: str) -> int:
    """0 = Tier-0 critical (bypasses the volume caps); 1 = normal."""
    return 0 if _TIER0_RE.search(task_name or "") else 1


def _record_deferred(task_gid: str, task_name: str, assignee: str,
                     tier: int, reason: str, run_id: str) -> None:
    """Append a cap-deferred nudge to hygiene-deferred.jsonl (informational).

    Recovery is automatic: the next run re-sorts Tier-0 first and re-evaluates,
    so a deferred task resurfaces — this ledger is for visibility into whether the
    volume cap is ever a real problem, not a recovery queue.
    """
    try:
        DEFERRED_FILE.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "run_id": run_id,
            "task_gid": task_gid,
            "task_name": task_name[:120],
            "assignee": assignee,
            "tier": tier,
            "reason": reason,
        }
        with DEFERRED_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as exc:  # never let logging block a run
        log.warning("Could not record deferred nudge for %s: %s", task_gid, exc)


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
    skipped_closed = 0
    total_nudges = 0    # Tier-1 nudges this run
    tier0_nudges = 0    # Tier-0 (critical) nudges this run
    deferred = 0        # nudge-eligible tasks cut by a volume cap

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

        # Tier-0 (compliance / LEX-revalidation / P0 / urgent) FIRST so a count-cap
        # day never starves a critical nudge (WS10). Stable sort keeps Asana order
        # within a tier.
        tasks = sorted(tasks, key=lambda t: _importance_tier(t.get("name", "")))

        user_nudges = 0
        user_tier0_nudges = 0
        for task in tasks:
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

            # Asana system-reminder skip (non-actionable auto-generated tasks,
            # e.g. "It's time to update your goal(s)")
            if _is_system_noise_task(task_name):
                log.debug("Skipping Asana system-reminder task: %s", task_name)
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

            # Importance-aware volume budget (WS10). Decide BEFORE the expensive
            # KB-signal query + the LIVE-Asana closed_task_guard call, so a
            # cap-deferred task does not trigger an API/DB hit (D-051 review: the
            # old post-guard placement issued unbounded live Asana reads on a
            # backlog day). Tier-0 (critical) bypasses the Tier-1 caps, bounded by
            # MAX_TIER0 + MAX_TIER0_PER_USER; Tier-1 respects MAX_TOTAL +
            # MAX_PER_USER. Cap-cut tasks are logged to hygiene-deferred.jsonl
            # (informational; recovery is automatic — next run re-sorts Tier-0 first).
            tier = _importance_tier(task_name)
            if tier == 0:
                if tier0_nudges >= MAX_TIER0 or user_tier0_nudges >= MAX_TIER0_PER_USER:
                    if not dry_run:
                        _record_deferred(task_gid, task_name, display_name, tier, "tier0_cap", run_id)
                    deferred += 1
                    continue
            else:
                if total_nudges >= MAX_TOTAL or user_nudges >= MAX_PER_USER:
                    if not dry_run:
                        _record_deferred(task_gid, task_name, display_name, tier, "volume_cap", run_id)
                    deferred += 1
                    continue

            # KB signal check
            if _has_kb_signal(task_name):
                log.debug("Skipping task with recent KB signal: %s", task_name)
                skipped_signal += 1
                continue

            # Fire-time completion guard (2026-06-11 Hannah report): the
            # candidate list filters on completion, but the task can close
            # between listing and firing -- and the shared ledger must record
            # already-closed tasks so NO source (this job, the weekly sweep)
            # ever nudges them again. Skipped in dry-run (read-only preview;
            # the guard writes ledger rows). Fails open inside the guard.
            if not dry_run and closed_task_guard(
                task_gid, task_name=task_name, run_id=run_id
            ):
                skipped_closed += 1
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
            if tier == 0:
                tier0_nudges += 1
                user_tier0_nudges += 1
            else:
                user_nudges += 1
                total_nudges += 1

    if not dry_run and nudges_sent > 0:
        _save_throttle(throttle)

    return {
        "tasks_checked": tasks_checked,
        "nudges_sent": nudges_sent,
        "tier0_nudges": tier0_nudges,
        "skipped_throttle": skipped_throttle,
        "skipped_signal": skipped_signal,
        "skipped_closed": skipped_closed,
        "deferred": deferred,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Asana Hygiene Nudges")
    parser.add_argument("--dry-run", action="store_true", help="Log without posting comments")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run)
    log.info("asana_hygiene_nudges result: %s", result)
