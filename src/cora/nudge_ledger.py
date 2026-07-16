"""Shared Asana nudge ledger -- one source of truth across both nudge systems.

Two systems comment on stale Asana tasks:
  1. The daily Cora "Asana Hygiene Nudges" job (this repo, Tier 3).
  2. The weekly Cowork "hygiene-asana" closure-detection sweep (Step 4.6),
     which already reads + appends an append-only JSONL throttle log to
     enforce its own 7-day-per-task lockout.

To stop the two from double-nudging the same task (nudge fatigue), this module
makes the daily job read AND append that SAME closure-nudges JSONL. Because the
weekly system already consults that file for its lockout, sharing it gives a
bidirectional guarantee with zero change to the weekly SKILL: each system sees
the other's fires.

Doctrine: max 1 automated comment of ANY kind per task per 7 days. The daily
job applies a stricter 14-day cross-system lockout (see run_asana_hygiene_nudges).

Path is resolved from CLOSURE_NUDGE_LOG_PATH, defaulting to the Drive location
the weekly system writes to. Reads fail OPEN (missing/unreadable -> "not
recently nudged") so a Drive hiccup never blocks the daily job; writes are
best-effort and never raise.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from . import drive_io

log = logging.getLogger(__name__)


def _safe_exists(path: Path) -> bool:
    """Bounded, fail-open existence check. The ledger lives on the G: mount; a
    transient unmount returns False ("not present" -> the callers treat this as
    "not recently nudged" / "start a fresh count"), matching the module's fail-open
    doctrine, and can never hang the daily nudge job."""
    try:
        return drive_io.exists(path)
    except drive_io.DriveUnavailable:
        log.warning("nudge_ledger: G: mount unavailable checking %s -- failing open", path)
        return False

_DEFAULT_LOG_PATH = (
    r"G:\My Drive\HJR-Founder-OS\_shared\projects\cora\closure-nudges-throttle.jsonl"
)

# A task completed longer ago than this is treated as deliberately archived and
# permanently excluded from all future nudges (2026-06-11 fix: Hannah was
# nudged daily on a task closed a year prior -- the guards only checked
# throttle history, never live completion state).
CLOSED_PERMANENT_AFTER_HOURS = 48
_CLOSED_REASON = "already_closed"


def _log_path() -> Path:
    return Path(os.environ.get("CLOSURE_NUDGE_LOG_PATH") or _DEFAULT_LOG_PATH)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _iter_rows(path: Path):
    """Yield parsed data rows (skipping the schema header + blank/bad lines)."""
    try:
        # G: mount: drive_io makes a transient unmount a bounded DriveUnavailable
        # (an OSError, caught here -> fail-open empty iteration) instead of a hang.
        text = drive_io.read_text(path, encoding="utf-8")
    except Exception:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict) or "_schema" in row:
            continue
        yield row


def recently_nudged(task_gid: str, within_days: int = 14) -> bool:
    """True if `task_gid` was nudged by EITHER system within `within_days`.

    Fail-open: any read error / missing file returns False.
    """
    if not task_gid:
        return False
    path = _log_path()
    if not _safe_exists(path):
        return False

    now = datetime.now(timezone.utc)
    cutoff_secs = within_days * 86400
    try:
        for row in _iter_rows(path):
            if str(row.get("task_gid")) != str(task_gid):
                continue
            ts = _parse_iso(row.get("last_nudged_at"))
            if ts is None:
                continue
            if (now - ts).total_seconds() < cutoff_secs:
                return True
    except Exception as exc:
        log.warning("nudge_ledger read failed (%s) -- failing open", exc)
        return False
    return False


def permanently_excluded(task_gid: str) -> bool:
    """True if the ledger holds a permanent already_closed exclusion for the task.

    Once recorded, the task is never re-evaluated by any nudge source that
    consults this ledger. Fail-open: read errors return False.
    """
    if not task_gid:
        return False
    path = _log_path()
    if not _safe_exists(path):
        return False
    try:
        for row in _iter_rows(path):
            if (
                str(row.get("task_gid")) == str(task_gid)
                and row.get("reason") == _CLOSED_REASON
                and row.get("permanent")
            ):
                return True
    except Exception as exc:
        log.warning("nudge_ledger exclusion read failed (%s) -- failing open", exc)
        return False
    return False


def record_closed_skip(
    task_gid: str,
    *,
    task_name: str = "",
    completed_at: str = "",
    permanent: bool = True,
    run_id: str = "",
    signal_source: str = "closed_task_guard",
) -> bool:
    """Record that a nudge was skipped because the task is already completed.

    The row carries `last_nudged_at` (= the skip time) on purpose: the weekly
    Cowork sweep applies its lockout window to that field when it reads this
    file, so a recorded skip also suppresses the other system without any
    change to its SKILL. `reason`/`permanent` mark the row so
    permanently_excluded() never re-evaluates the task. Best-effort.
    """
    if not task_gid:
        return False
    path = _log_path()
    try:
        row = {
            "task_gid": str(task_gid),
            "task_name": task_name,
            "last_nudged_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "nudge_count": _prior_nudge_count(path, task_gid) if _safe_exists(path) else 0,
            "reason": _CLOSED_REASON,
            "permanent": bool(permanent),
            "completed_at": completed_at or "",
            "signal_source": signal_source,
            "signal_summary": "Task already completed at fire time -- nudge skipped.",
            "run_id": run_id,
        }
        # G: append, timeout-bounded (single attempt, no retry -> no double-append).
        # A gone mount raises DriveUnavailable (caught below -> fail-open, skip record).
        drive_io.append_text(path, json.dumps(row) + "\n")
        return True
    except Exception as exc:
        log.warning("nudge_ledger closed-skip append failed for task %s: %s", task_gid, exc)
        return False


def closed_task_guard(task_gid: str, *, task_name: str = "", run_id: str = "") -> bool:
    """Fire-time completion guard. True = task is closed, skip the nudge.

    Order of checks:
      1. Ledger already holds a permanent already_closed row -> True (no API call).
      2. Fetch live completed/completed_at from Asana. On ANY fetch error,
         fail open (False -- the nudge proceeds; if Asana is down the comment
         post will fail loudly on its own).
      3. Completed -> record a skip row and return True. The exclusion is
         permanent when completed_at is older than CLOSED_PERMANENT_AFTER_HOURS
         (or missing/unparseable -- a completed task with no timestamp is
         assumed long-closed); a just-closed task (<48h) only gets a throttled
         row so the window re-evaluates later.
    """
    if not task_gid:
        return False
    if permanently_excluded(task_gid):
        return True

    try:
        from cora.tools.asana_client import get_task_completion
        state = get_task_completion(task_gid)
    except Exception as exc:
        log.warning(
            "closed_task_guard fetch failed for task %s (%s) -- failing open",
            task_gid, exc,
        )
        return False

    if not state.get("completed"):
        return False

    completed_at = state.get("completed_at") or ""
    completed_dt = _parse_iso(completed_at)
    if completed_dt is None:
        permanent = True
    else:
        age_secs = (datetime.now(timezone.utc) - completed_dt).total_seconds()
        permanent = age_secs > CLOSED_PERMANENT_AFTER_HOURS * 3600
    record_closed_skip(
        task_gid,
        task_name=task_name,
        completed_at=completed_at,
        permanent=permanent,
        run_id=run_id,
    )
    log.info(
        "closed_task_guard: task %s already completed (%s) -- nudge skipped%s",
        task_gid, completed_at or "no completed_at",
        " [permanent exclusion]" if permanent else "",
    )
    return True


def _prior_nudge_count(path: Path, task_gid: str) -> int:
    highest = 0
    for row in _iter_rows(path):
        if str(row.get("task_gid")) == str(task_gid):
            try:
                highest = max(highest, int(row.get("nudge_count") or 0))
            except Exception:
                continue
    return highest


def record_nudge(
    task_gid: str,
    *,
    task_name: str = "",
    assignee_user: str = "",
    assignee_email: str = "",
    assignee_gid: str = "",
    confidence: str = "MEDIUM",
    signal_source: str = "task_staleness_daily",
    signal_summary: str = "",
    run_id: str = "",
) -> bool:
    """Append a nudge row to the shared closure-nudges JSONL. Best-effort.

    Increments nudge_count off the highest prior count seen for the task.
    Returns True on success, False on any failure (never raises).
    """
    if not task_gid:
        return False
    path = _log_path()
    try:
        count = _prior_nudge_count(path, task_gid) + 1 if _safe_exists(path) else 1
        row = {
            "task_gid": str(task_gid),
            "task_name": task_name,
            "assignee_user": assignee_user,
            "assignee_email": assignee_email,
            "assignee_gid": str(assignee_gid),
            "last_nudged_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "nudge_count": count,
            "confidence": confidence,
            "signal_source": signal_source,
            "signal_summary": signal_summary,
            "run_id": run_id,
        }
        # G: append, timeout-bounded (single attempt, no retry -> no double-append).
        # A gone mount raises DriveUnavailable (caught below -> fail-open, skip record).
        drive_io.append_text(path, json.dumps(row) + "\n")
        return True
    except Exception as exc:
        log.warning("nudge_ledger append failed for task %s: %s", task_gid, exc)
        return False
