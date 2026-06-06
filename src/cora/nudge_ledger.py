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

log = logging.getLogger(__name__)

_DEFAULT_LOG_PATH = (
    r"G:\My Drive\HJR-Founder-OS\_shared\projects\cora\closure-nudges-throttle.jsonl"
)


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
        text = path.read_text(encoding="utf-8")
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
    if not path.exists():
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
        count = _prior_nudge_count(path, task_gid) + 1 if path.exists() else 1
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
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return True
    except Exception as exc:
        log.warning("nudge_ledger append failed for task %s: %s", task_gid, exc)
        return False
