"""Missed-start catch-up for the nightly ingest set -- the I/O half (Code #13 slice 2,
cq-fb50c9e6c911). Decisions live in src/cora/nightly_catchup.py (pure, tested).

Schedule: daily 08:30 AZ as "Cora - Missed Nightly Catch-Up"
(deployment/setup-missed-nightly-catchup-task.ps1; ExecutionTimeLimit 4h). Dry-run by
default; the registered task passes --apply.

    .venv\\Scripts\\python.exe scripts\\check_missed_nightly.py                 # plan for today, writes nothing
    .venv\\Scripts\\python.exe scripts\\check_missed_nightly.py --day 2026-09-09  # replay a past window (dry-run ONLY)
    .venv\\Scripts\\python.exe scripts\\check_missed_nightly.py --apply           # replay today's misses, in order

WHAT --apply DOES, per task the decision engine marks `run` (trigger order):
  1. reads the task's LIVE registered action from Task Scheduler and strips the
     run_hidden prefix, so the replay is the exact registered child command (falls
     back to the yaml `command` when the action is unreadable);
  2. spawns it through deployment/run_hidden.py under pythonw.exe (windowless,
     D-266..D-269; the command line is passed as ONE STRING so the child survives
     byte-for-byte), waits synchronously up to the task's max_minutes;
  3. appends a `task` row to the catch-up ledger (rc, duration) -- NEVER twice for one
     window; the plan row records every decision, including the ones not replayed.
  Finally ONE run marker for this lane with `catch_up: true` + the window + the tasks.

WHAT IT NEVER DOES: run outside 06:00-12:00 AZ; run a task Task Scheduler reports
RUNNING or Disabled; run a task whose next trigger is imminent; run for a past
window with --apply (refused); consult any model.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import nightly_catchup as nc  # noqa: E402
from cora import run_marker  # noqa: E402

log = logging.getLogger("check_missed_nightly")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PYTHONW = _REPO_ROOT / ".venv" / "Scripts" / "pythonw.exe"
LAUNCHER = _REPO_ROOT / "deployment" / "run_hidden.py"
SCRIPT_NAME = "check_missed_nightly.py"


# ── Task Scheduler reads (one PowerShell call for the whole set) ─────────────
def read_task_states(names: list[str], *, timeout: float = 60.0) -> tuple[dict[str, nc.TaskState], dict[str, str]] | None:
    """{name: TaskState}, {name: live child command}. None when the query itself
    failed (the decision engine then reads every candidate as cannot_check)."""
    if not names:
        return {}, {}
    quoted = ",".join("'" + n.replace("'", "''") + "'" for n in names)
    ps = (
        "$names = @(" + quoted + "); foreach ($n in $names) { "
        "$t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue; "
        "if ($t) { $i = $t | Get-ScheduledTaskInfo; $a = $t.Actions[0]; "
        "$nr = if ($i.NextRunTime) { $i.NextRunTime.ToString('yyyy-MM-ddTHH:mm:ss') } else { '' }; "
        "$lr = if ($i.LastRunTime) { $i.LastRunTime.ToString('yyyy-MM-ddTHH:mm:ss') } else { '' }; "
        "Write-Output ($n + '|' + $t.State + '|' + $nr + '|' + $lr + '|' + $a.Execute + '|' + $a.Arguments) } "
        "else { Write-Output ($n + '|MISSING||||') } }"
    )
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=timeout,
                             creationflags=_NO_WINDOW).stdout
    except Exception as exc:  # noqa: BLE001
        log.warning("Task Scheduler query failed: %s", exc)
        return None
    return parse_task_states(out)


def parse_task_states(out: str) -> tuple[dict[str, nc.TaskState], dict[str, str]]:
    states: dict[str, nc.TaskState] = {}
    actions: dict[str, str] = {}
    for line in (out or "").splitlines():
        line = line.rstrip("\r").strip()
        if not line or "|" not in line:
            continue
        parts = line.split("|", 5)
        if len(parts) < 6:
            parts += [""] * (6 - len(parts))
        name, state, nr, lr, execute, arguments = (p.strip() for p in parts)
        if not name:
            continue
        if state.upper() == "MISSING" or not state:
            states[name] = nc.TaskState(state=None)
            continue
        states[name] = nc.TaskState(state=state, next_run=_parse_local(nr), last_run=_parse_local(lr))
        child = nc.child_command_from_action(execute, arguments)
        if child:
            actions[name] = child
    return states, actions


def _parse_local(s: str) -> datetime | None:
    """Task Scheduler stamps are host-local; the host is Arizona (no DST)."""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=nc.AZ)
    except ValueError:
        return None


# ── replay ───────────────────────────────────────────────────────────────────
def replay(task: nc.NightlyTask, child: str, *, popen=subprocess.Popen) -> dict:
    """Run ONE task through run_hidden and wait; returns the ledger row body."""
    cmdline = nc.launcher_command_line(PYTHONW, LAUNCHER, task.slug, child)
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    try:
        proc = popen(cmdline, creationflags=_NO_WINDOW, cwd=str(_REPO_ROOT))
    except Exception as exc:  # noqa: BLE001
        return {"action": "error", "rc": None, "error": f"spawn failed: {exc}", "command": child,
                "started": started.isoformat(timespec="seconds"), "duration_s": 0}
    action, rc = "ran", None
    try:
        rc = proc.wait(timeout=task.max_minutes * 60)
        if rc != 0:
            action = "error"
    except subprocess.TimeoutExpired:
        action = "timeout"
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    return {"action": action, "rc": rc, "command": child,
            "started": started.isoformat(timespec="seconds"),
            "ended": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "duration_s": round(time.monotonic() - t0, 1)}


def main(argv: list[str] | None = None, *, now_az: datetime | None = None,
         states_reader=read_task_states, popen=subprocess.Popen) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--day", help="window date YYYY-MM-DD (AZ); default today. Past days are dry-run ONLY.")
    parser.add_argument("--apply", action="store_true", help="replay today's misses (default: print the plan)")
    parser.add_argument("--set", default=str(nc.DEFAULT_SET_PATH), help="nightly set yaml")
    parser.add_argument("--log-dir", default=str(nc.DEFAULT_TASK_LOG_DIR), help="logs/tasks dir")
    parser.add_argument("--no-scheduler", action="store_true",
                        help="skip the Task Scheduler query (dry-run replays of past days)")
    parser.add_argument("--json", action="store_true", help="print the plan as JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])

    now_az = now_az or datetime.now(nc.AZ)
    day = date.fromisoformat(args.day) if args.day else now_az.date()
    if args.apply and day != now_az.date():
        print(f"REFUSED: --apply is for today's window only ({now_az.date().isoformat()}); "
              f"{day.isoformat()} is a dry-run replay.")
        return 2

    window, tasks = nc.load_set(Path(args.set))
    if not tasks:
        print(f"no tasks in the set at {args.set} -- nothing to check")
        return 1
    log_dir = Path(args.log_dir)
    markers = run_marker.read_markers()

    def evidence(task: nc.NightlyTask) -> list[datetime]:
        return nc.header_stamps(log_dir, task.slug, day) + nc.marker_stamps(markers, task.name, day)

    states: dict[str, nc.TaskState] | None
    actions: dict[str, str] = {}
    if args.no_scheduler:
        states = {t.name: nc.TaskState(state="Ready") for t in tasks}
    else:
        read = states_reader([t.name for t in tasks])
        if read is None:
            states = None
        else:
            states, actions = read

    ledger_rows = nc.read_ledger()
    decisions = nc.decide(tasks, window=window, day=day, now_az=now_az, evidence=evidence,
                          states=states, ledger_rows=ledger_rows)
    mode = "apply" if args.apply else "dry-run"
    if args.json:
        print(json.dumps({"window_date": day.isoformat(), "mode": mode,
                          "decisions": [d.as_row() for d in decisions]}, indent=1))
    else:
        print(f"Missed-nightly catch-up -- window {day.isoformat()} -- {mode} -- now {now_az.strftime('%H:%M')} AZ")
        print(nc.format_plan(decisions))
    if not args.apply:
        return 0

    nc.append_ledger(nc.plan_row(day, now_az, mode, decisions))
    replayed: list[str] = []
    ok = True
    for d in decisions:
        if d.action != "run":
            continue
        child = actions.get(d.task.name) or d.task.command
        row = {"row": "task", "window_date": day.isoformat(), "task": d.task.name,
               "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        if not child:
            row.update({"action": "error", "rc": None, "error": "no registered action and no fallback command"})
            ok = False
        else:
            log.info("replaying %s (trigger %s AZ) via run_hidden", d.task.name, d.task.trigger_az)
            row.update(replay(d.task, child, popen=popen))
            ok = ok and row["action"] == "ran"
        nc.append_ledger(row)
        replayed.append(f"{d.task.name}:{row['action']}")
        print(f"  {row['action']:<8} {d.task.name} rc={row.get('rc')} {row.get('duration_s', '')}s")
    run_marker.write(
        nc.TASK_NAME, script=SCRIPT_NAME, ok=ok, outputs=len(replayed),
        outcome="replayed" if replayed else "nothing-missed",
        detail="window=%s %s" % (day.isoformat(), ", ".join(replayed) or "all fired"),
        extra={"catch_up": True, "window": day.isoformat(), "replayed": replayed,
               "counts": nc.counts(decisions)},
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
