"""Missed-start catch-up for the nightly ingest set -- the I/O half (Code #13 slice 2,
cq-fb50c9e6c911). Decisions live in src/cora/nightly_catchup.py (pure, tested).

Schedule: daily 08:30 AZ as "Cora - Missed Nightly Catch-Up"
(deployment/setup-missed-nightly-catchup-task.ps1; ExecutionTimeLimit 4h). Dry-run by
default. The lane is T1 (ruling 9.2, 2026-09-19): the registered task passes
--record, which RECORDS today's dry-run plan (one plan row, mode=dry-run, plus a
plan-only run marker) and replays nothing, so the 08:45 health check can tell "fired,
nothing to replay" from "did not fire". The setup PS1's -Apply switch registers T2
(--apply) instead. A plain run with neither flag still writes NOTHING.

    .venv\\Scripts\\python.exe scripts\\check_missed_nightly.py                 # plan for today, writes nothing
    .venv\\Scripts\\python.exe scripts\\check_missed_nightly.py --record        # T1: record today's plan, replay nothing
    .venv\\Scripts\\python.exe scripts\\check_missed_nightly.py --day 2026-09-09  # replay a past window (dry-run ONLY)
    .venv\\Scripts\\python.exe scripts\\check_missed_nightly.py --day 2026-09-09 --now 08:45   # ... as of 08:45 that day
    .venv\\Scripts\\python.exe scripts\\check_missed_nightly.py --apply           # replay today's misses, in order

A PAST --day is evaluated as of that day's window end minus one second (11:59:59
AZ) unless --now HH:MM pins another moment: the live clock is always outside a past
day's window, so evaluating "now" would read every unfired task skipped_window and
the advertised 9/9 dry-run could never show a single `run` (D-051 review B-3). The
banner says which clock was used. --apply is keyed on the REAL clock only.

WHAT --apply DOES, per task the decision engine marks `run` (trigger order):
  1. reads the task's LIVE registered action from Task Scheduler and strips the
     run_hidden prefix, so the replay is the exact registered child command (falls
     back to the yaml `command` when the action is unreadable); the log slug is the
     action's own `--name <slug>` (Get-TaskSlug's output, e.g. Cora-Drive-Sweep),
     so the evidence read and the replay header share the launcher's real file;
  2. RE-CHECKS at the moment of the spawn (earlier replays can run for hours):
     still before the window end, next trigger not imminent, not RUNNING, and the
     lane's own budget (nightly_catchup.LANE_BUDGET_MIN = the task's 4h limit) not
     exhausted -- on failure a `task` row with skipped_window / skipped_imminent /
     cannot_check / not-started is written INSTEAD of a spawn;
  3. spawns it through deployment/run_hidden.py under pythonw.exe (windowless,
     D-266..D-269; the command line is passed as ONE STRING so the child survives
     byte-for-byte), waits synchronously up to the task's max_minutes, capped by the
     budget left;
  4. appends a `task` row to the catch-up ledger (rc, duration) -- NEVER twice for one
     window; the plan row records every decision, including the ones not replayed.
  Finally ONE run marker for this lane with `catch_up: true` + the window + the tasks.

WHAT IT NEVER DOES: spawn outside 06:00-12:00 AZ (checked per spawn, not once);
run a task Task Scheduler reports RUNNING or Disabled; run a task whose next
trigger is imminent; run for a past window with --apply (refused, exit 2); --apply
without the live scheduler read (--no-scheduler) or with a pinned clock (--now) --
both refused, exit 2, because the window / imminent / RUNNING / slug rules would
run blind; --record under the same conditions (a past --day, --now, --no-scheduler)
or together with --apply -- refused, exit 2, because a recorded plan is read as the
lane's fire and must be the live one; consult any model.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

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
        states[name] = nc.TaskState(state=state, next_run=_parse_local(nr), last_run=_parse_local(lr),
                                    slug=nc.slug_from_action(arguments))
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
def replay(task: nc.NightlyTask, child: str, *, slug: str | None = None,
           timeout_s: float | None = None, popen=subprocess.Popen) -> dict:
    """Run ONE task through run_hidden and wait; returns the ledger row body.
    *slug* = the live action's --name (default: the task's Get-TaskSlug rule);
    *timeout_s* caps the wait below max_minutes when the lane budget is shorter."""
    slug = slug or task.slug
    cmdline = nc.launcher_command_line(PYTHONW, LAUNCHER, slug, child)
    started = datetime.now(timezone.utc)
    t0 = time.monotonic()
    try:
        proc = popen(cmdline, creationflags=_NO_WINDOW, cwd=str(_REPO_ROOT))
    except Exception as exc:  # noqa: BLE001
        return {"action": "error", "rc": None, "error": f"spawn failed: {exc}", "command": child, "slug": slug,
                "started": started.isoformat(timespec="seconds"), "duration_s": 0}
    action, rc = "ran", None
    wait_s = task.max_minutes * 60
    if timeout_s is not None:
        wait_s = max(1, min(wait_s, int(timeout_s)))
    try:
        rc = proc.wait(timeout=wait_s)
        if rc != 0:
            action = "error"
    except subprocess.TimeoutExpired:
        action = "timeout"
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    return {"action": action, "rc": rc, "command": child, "slug": slug,
            "started": started.isoformat(timespec="seconds"),
            "ended": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "duration_s": round(time.monotonic() - t0, 1)}


def _parse_hhmm(s: str) -> tuple[int, int]:
    hh, mm = s.strip().split(":", 1)
    hh_i, mm_i = int(hh), int(mm)
    if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
        raise ValueError(s)
    return hh_i, mm_i


def main(argv: list[str] | None = None, *, now_az: datetime | None = None,
         states_reader=read_task_states, popen=subprocess.Popen,
         clock: Callable[[], datetime] | None = None) -> int:
    """*now_az* = the REAL clock (tests inject it; None = datetime.now(AZ)). *clock* =
    the live clock the replay loop re-reads before EACH spawn (default: the real
    clock, or the injected now_az frozen when a test injects one)."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--day", help="window date YYYY-MM-DD (AZ); default today. Past days are dry-run ONLY.")
    parser.add_argument("--apply", action="store_true", help="replay today's misses (default: print the plan)")
    parser.add_argument("--record", action="store_true",
                        help="T1: RECORD today's dry-run plan (one plan row, mode=dry-run, + a "
                             "plan-only run marker) and replay nothing. Today's live window on "
                             "the real clock only; mutually exclusive with --apply")
    parser.add_argument("--set", default=str(nc.DEFAULT_SET_PATH), help="nightly set yaml")
    parser.add_argument("--log-dir", default=str(nc.DEFAULT_TASK_LOG_DIR), help="logs/tasks dir")
    parser.add_argument("--no-scheduler", action="store_true",
                        help="skip the Task Scheduler query (dry-run ONLY; refused with --apply)")
    parser.add_argument("--now", metavar="HH:MM",
                        help="evaluate the plan as of this AZ time on the window day (dry-run ONLY; "
                             "default for a past --day: the window end minus one second)")
    parser.add_argument("--json", action="store_true", help="print the plan as JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])

    real_now = now_az or datetime.now(nc.AZ)
    if clock is None:
        clock = (lambda: now_az) if now_az is not None else (lambda: datetime.now(nc.AZ))
    day = date.fromisoformat(args.day) if args.day else real_now.date()
    # --apply refusals are keyed on the REAL clock and the REAL scheduler, never a pin
    if args.apply and day != real_now.date():
        print(f"REFUSED: --apply is for today's window only ({real_now.date().isoformat()}); "
              f"{day.isoformat()} is a dry-run replay.")
        return 2
    if args.apply and args.no_scheduler:
        print("REFUSED: --apply needs the live Task Scheduler read (--no-scheduler assumes every task "
              "Ready with no live --name slug: a fired task would be replayed and its header would land "
              "in the wrong log file). Drop --no-scheduler, or drop --apply for a dry-run.")
        return 2
    if args.apply and args.now:
        print("REFUSED: --now pins the clock for dry-runs only; a live replay evaluates the real clock.")
        return 2
    # --record (Code #14 R14-7 4a, ruling 9.2 = T1): the SAME refusals as --apply.
    # A recorded plan is what the 08:45 health check reads as "the lane fired", so
    # it must describe today's LIVE window on the REAL clock and the REAL scheduler
    # -- a pinned --day/--now or a --no-scheduler guess would write a false record.
    if args.record and args.apply:
        print("REFUSED: --record (T1: record the plan, replay nothing) and --apply (T2: replay) "
              "are mutually exclusive.")
        return 2
    if args.record and day != real_now.date():
        print(f"REFUSED: --record is for today's window only ({real_now.date().isoformat()}); "
              f"{day.isoformat()} is a dry-run replay and writes nothing.")
        return 2
    if args.record and args.no_scheduler:
        print("REFUSED: --record needs the live Task Scheduler read (--no-scheduler assumes every "
              "task Ready); a recorded plan must be the real one. Drop --no-scheduler.")
        return 2
    if args.record and args.now:
        print("REFUSED: --now pins the clock for dry-runs only; a recorded plan evaluates the real clock.")
        return 2

    try:
        window, tasks = nc.load_set(Path(args.set))
    except ValueError as exc:
        print(f"REFUSED: {exc}")
        return 2
    if not tasks:
        print(f"no tasks in the set at {args.set} -- nothing to check")
        return 1

    eval_now = real_now
    banner_note = ""
    if args.now:
        try:
            hh, mm = _parse_hhmm(args.now)
        except ValueError:
            print(f"REFUSED: --now must be HH:MM (got {args.now!r})")
            return 2
        eval_now = datetime(day.year, day.month, day.day, hh, mm, tzinfo=nc.AZ)
        banner_note = f" -- evaluated as of {eval_now.strftime('%H:%M:%S')} AZ on {day.isoformat()} (--now)"
    elif day < real_now.date():
        eval_now = window.end_dt(day) - timedelta(seconds=1)
        banner_note = (f" -- past window: evaluated as of {eval_now.strftime('%H:%M:%S')} AZ on {day.isoformat()}"
                       f" (real clock {real_now.strftime('%Y-%m-%d %H:%M')} AZ)")

    log_dir = Path(args.log_dir)
    markers = run_marker.read_markers()

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

    def evidence(task: nc.NightlyTask) -> list[datetime]:
        # the live action's --name is the launcher's real log filename (Cora-Drive-Sweep,
        # never Cora---Drive-Sweep); the Get-TaskSlug rule is the fallback
        slug = nc.effective_slug(task, (states or {}).get(task.name))
        return nc.header_stamps(log_dir, slug, day) + nc.marker_stamps(markers, task.name, day)

    ledger_rows = nc.read_ledger()
    decisions = nc.decide(tasks, window=window, day=day, now_az=eval_now, evidence=evidence,
                          states=states, ledger_rows=ledger_rows)
    mode = "apply" if args.apply else "dry-run"
    if args.json:
        print(json.dumps({"window_date": day.isoformat(), "mode": mode,
                          "evaluated_at": eval_now.isoformat(timespec="seconds"),
                          "decisions": [d.as_row() for d in decisions]}, indent=1))
    else:
        print(f"Missed-nightly catch-up -- window {day.isoformat()} -- {mode} -- now {eval_now.strftime('%H:%M')} AZ"
              + banner_note)
        print(nc.format_plan(decisions))
    if args.record:
        # T1: the plan is the record. mode stays "dry-run" (nothing was replayed);
        # the health check reads a dry-run plan row as "fired (T1 plan recorded)".
        nc.append_ledger(nc.plan_row(day, eval_now, "dry-run", decisions))
        would = [d.task.name for d in decisions if d.action == "run"]
        run_marker.write(
            nc.TASK_NAME, script=SCRIPT_NAME, ok=True, outputs=0, outcome="plan-only",
            detail="window=%s T1 plan recorded; %s" % (
                day.isoformat(),
                ("would have replayed " + ", ".join(would)) if would else "nothing to replay"),
            extra={"catch_up": True, "window": day.isoformat(), "mode": "dry-run",
                   "would_replay": would, "counts": nc.counts(decisions)},
        )
        print(f"recorded the T1 plan for {day.isoformat()} (mode=dry-run; nothing replayed)")
        return 0
    if not args.apply:
        return 0

    nc.append_ledger(nc.plan_row(day, eval_now, mode, decisions))
    lane_start = clock()
    budget_s = nc.LANE_BUDGET_MIN * 60
    replayed: list[str] = []
    ok = True
    for d in decisions:
        if d.action != "run":
            continue
        row = {"row": "task", "window_date": day.isoformat(), "task": d.task.name,
               "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        # re-check at the moment of the spawn: window end, budget, RUNNING, imminent
        # (re-read from the scheduler; the plan-time state is the fallback)
        spawn_now = clock()
        state = (states or {}).get(d.task.name)
        reread = states_reader([d.task.name])
        if reread is not None and reread[0].get(d.task.name) is not None and reread[0][d.task.name].state:
            state = reread[0][d.task.name]
        budget_left = budget_s - (spawn_now - lane_start).total_seconds()
        deferred = nc.recheck_before_spawn(d.task, window=window, day=day, now_az=spawn_now,
                                           state=state, budget_left_s=budget_left)
        if deferred is not None:
            action, reason = deferred
            row.update({"action": action, "rc": None, "reason": reason})
            ok = ok and action == "skipped_imminent"   # the scheduler's own fire covers that one
            nc.append_ledger(row)
            replayed.append(f"{d.task.name}:{action}")
            log.warning("NOT spawning %s: %s", d.task.name, reason)
            print(f"  {action:<16} {d.task.name} -- {reason}")
            continue
        child = actions.get(d.task.name) or d.task.command
        if not child:
            row.update({"action": "error", "rc": None, "error": "no registered action and no fallback command"})
            ok = False
        else:
            slug = nc.effective_slug(d.task, state)
            log.info("replaying %s (trigger %s AZ, slug %s) via run_hidden", d.task.name, d.task.trigger_az, slug)
            row.update(replay(d.task, child, slug=slug, timeout_s=budget_left, popen=popen))
            ok = ok and row["action"] == "ran"
        nc.append_ledger(row)
        replayed.append(f"{d.task.name}:{row['action']}")
        print(f"  {row['action']:<16} {d.task.name} rc={row.get('rc')} {row.get('duration_s', '')}s")
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
