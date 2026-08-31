"""Run-marker contract (session #11 S4) -- retires the cq-a251dee3f5cf CLASS.

THE CLASS: "a task that fires and writes nothing is indistinguishable from one
that never fired." The weekly Slack-clarity check FIRED on 8/22 (registry-confirmed
lastRunAt) and posted no digest; nothing anywhere recorded that. Task Scheduler
knows a task RAN and its exit code; it cannot know the run produced nothing.

WHAT A MARKER IS. One append-only JSON line per run: which task, when, whether it
succeeded, and -- the whole point -- HOW MANY OUTPUTS it actually wrote or sent.
`outputs` is an INT, not a bool: a run that posted 0 messages and wrote 0 files is
the alarm, even when it exited 0.

STORAGE. ONE append-only ledger, not a per-task JSON file. At fleet scale (93
tasks, 93 separate processes) per-task files mean the reader has to know all their
names, which is the registry problem again. Append-only with no read-modify-write
is the repo's ledger doctrine and the only multi-process-safe shape here -- a lock
would be process-local and useless across 93 processes (see
meeting_capture.write_ledger, whose contract this copies).

WHY logs/ AND NOT data/state/. compact_logs.py globs LOGS_DIR and DATA_DIR
NON-recursively, so data/state/*.jsonl is never trimmed and would grow forever,
while logs/*.jsonl is trimmed only above 5MB keeping 90 days by `ts` -- far beyond
any cadence window.

WRITE FAILURE IS ITSELF LOUD (D-133). Observability that fails silently is worse
than none: it converts "no marker" into "task never ran". On failure this emits
RUN_MARKER_WRITE_FAILING, which nightly_health_check._CRITICAL_LOG_PATTERNS
matches (same precedent as HEARTBEAT_FILE_WRITE_FAILING), returns False, and never
raises into the caller -- a broken ledger must not take down the lane it watches.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PATH = _REPO_ROOT / "logs" / "task-runs.jsonl"

#: Emitted on write failure. nightly_health_check._CRITICAL_LOG_PATTERNS matches
#: this token, so a silently-failing marker writer becomes a CRITICAL rather than
#: a lane that merely looks like it never ran.
WRITE_FAIL_TOKEN = "RUN_MARKER_WRITE_FAILING"


def ledger_path() -> Path:
    """Overridable so tests never touch the real ledger."""
    return Path(os.environ.get("TASK_RUNS_LEDGER_PATH", "") or _DEFAULT_PATH)


def write(
    task: str,
    *,
    script: str = "",
    ok: bool = True,
    outputs: int = 0,
    outcome: str = "",
    detail: str = "",
    elapsed_s: float | None = None,
) -> bool:
    """Append one run marker. Returns True on success.

    `outputs` MUST be the count of things actually written or sent -- files
    created, messages posted, rows persisted. Passing a constant defeats the
    entire contract: the alarm this enables is "fired, exited 0, wrote nothing".

    Never raises. A marker write must not be able to fail a real lane.
    """
    row: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "task": str(task or "unknown"),
        "script": str(script or ""),
        "ok": bool(ok),
        "outputs": int(outputs or 0),
        "outcome": str(outcome or ("ok" if ok else "error")),
        "detail": str(detail or "")[:500],
        "pid": os.getpid(),
    }
    if elapsed_s is not None:
        try:
            row["elapsed_s"] = round(float(elapsed_s), 2)
        except (TypeError, ValueError):
            pass
    path = ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 -- observability must never raise
        log.error("%s task=%s path=%s err=%s", WRITE_FAIL_TOKEN, task, path, exc)
        return False


def read_markers(path: Path | None = None) -> list[dict]:
    """Every marker row. Malformed lines are skipped, never fatal."""
    p = Path(path) if path else ledger_path()
    if not p.exists():
        return []
    rows: list[dict] = []
    try:
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except Exception:  # noqa: BLE001
        return rows
    return rows


def latest_by_task(path: Path | None = None) -> dict[str, dict]:
    """Most recent marker per task name, by `ts`."""
    latest: dict[str, dict] = {}
    for row in read_markers(path):
        name = str(row.get("task") or "")
        if not name:
            continue
        prev = latest.get(name)
        if prev is None or str(row.get("ts") or "") > str(prev.get("ts") or ""):
            latest[name] = row
    return latest


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def evaluate(
    registry: list[dict],
    markers: dict[str, dict],
    *,
    now: datetime | None = None,
) -> list[tuple[str, str]]:
    """Diff expected cadence against observed markers.

    Returns (severity, message) pairs -- severity is "warn" or "ok".

    TWO distinct alarms, which is the point of the slice:
      * MISSED-FIRE   -- no marker inside the cadence window.
      * FIRED-NO-OUTPUT -- a marker exists and is recent, but outputs == 0 on a
        task declared `expects_output`. This is the cq-a251dee3f5cf shape and is
        invisible to Task Scheduler, which only ever sees exit code 0.

    A MISSING marker is NOT excused. The existing info-for-cora reader returns
    OK when its marker file is absent, which makes a never-adopted lane look
    healthy forever. Here the grace is gated on the `registered` date: a task
    registered before now-minus-cadence with no marker at all is an alarm, and a
    freshly registered one is quietly skipped until its first window elapses.
    """
    now = now or datetime.now(timezone.utc)
    out: list[tuple[str, str]] = []
    for entry in registry or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        try:
            cadence_h = float(entry.get("cadence_hours") or 0)
        except (TypeError, ValueError):
            cadence_h = 0.0
        if cadence_h <= 0:
            continue
        # generous window: a task is late only after 2x its cadence, so a single
        # skipped fire on a weekly job is not a nightly false alarm
        window_h = cadence_h * 2
        marker = markers.get(name)
        if marker is None:
            registered = _parse_ts(entry.get("registered") or "")
            if registered is not None:
                age_h = (now - registered).total_seconds() / 3600.0
                if age_h < window_h:
                    continue  # not yet due for its first marker
            out.append((
                "warn",
                f"{name}: no run marker ever recorded (cadence {cadence_h:.0f}h) -- "
                f"the task may not be firing, or has not adopted the marker helper",
            ))
            continue
        ts = _parse_ts(marker.get("ts") or "")
        if ts is None:
            out.append(("warn", f"{name}: run marker has an unparseable timestamp"))
            continue
        age_h = (now - ts).total_seconds() / 3600.0
        if age_h > window_h:
            out.append((
                "warn",
                f"{name}: last run marker is {age_h:.0f}h old (cadence {cadence_h:.0f}h) "
                f"-- MISSED FIRE",
            ))
            continue
        if entry.get("expects_output") and int(marker.get("outputs") or 0) == 0:
            out.append((
                "warn",
                f"{name}: FIRED BUT WROTE NOTHING ({ts.isoformat()}, outcome="
                f"{marker.get('outcome') or 'ok'}) -- exit code alone cannot see this",
            ))
            continue
        if not marker.get("ok", True):
            out.append((
                "warn",
                f"{name}: last run marker reports an error -- "
                f"{str(marker.get('detail') or '')[:120]}",
            ))
    return out
