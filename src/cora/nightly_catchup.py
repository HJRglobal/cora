"""Missed-start catch-up for the nightly ingest set -- the DECISION half (Code #13
slice 2, cq-fb50c9e6c911). scripts/check_missed_nightly.py is the I/O half.

THE DEFECT. 2026-09-09 00:27 -> 06:18 AZ the host was down. Every nightly task with
a trigger in that window (slack 02:00 ... Drive Sweep 06:00; 13 tasks) was lost;
Task Scheduler replayed NONE of them although each carried StartWhenAvailable;
the 08:45 health check reported nothing (its three task-facing checks read State,
the last exit code and a 4-task run-marker registry, none of which can see a fire
that never happened). Harrison hand-fired mirror/static/drive at 08:01 the next
day. The estate had no missed-start catch-up.

THE LANE (deterministic, no LLM). For each task in data/maps/nightly-catchup-set.yaml
(ORDER = registered trigger time ascending), for the current WINDOW DATE (an AZ day):

  fired          -- evidence of a fire inside [trigger - 10 min, window end):
                    a run marker for the task, a run_hidden header stamp in
                    logs/tasks/<slug>-<UTC date>.log, or Task Scheduler's own
                    LastRunTime. Only session-capture writes a marker today; the
                    header line is the load-bearing evidence for the other twelve
                    (VERIFY-FIRST 2026-09-19: a marker-only rule would replay 12 of
                    13 tasks every morning).
  skipped_not_due -- the deadline (trigger + grace) has not passed yet.
  skipped_window  -- now is outside 06:00-12:00 AZ (the midday set covers the rest;
                    mirror/static/capture have 12:15/12:20/12:30 triggers).
  skipped_already_ran -- this lane already replayed the task for this window
                    (its own ledger) -- NEVER twice for one window.
  cannot_check   -- Task Scheduler State unreadable, or the task is RUNNING (a
                    marker-less task with a live Running state is 'cannot check',
                    never 'run it': none of the nightly scripts holds a single-
                    instance lock, and IgnoreNew only protects scheduler launches).
  skipped_disabled -- the task is Disabled in Task Scheduler.
  skipped_imminent -- the task's next scheduled trigger is < imminent_min away
                    (the midday pair), so the scheduler is about to run it anyway.
  run            -- replay ONCE, synchronously, in trigger order, through
                    deployment/run_hidden.py with the task's LIVE registered child
                    command verbatim (windowless, D-266..D-269), bounded by
                    max_minutes; the replay writes its own ledger row + a run marker
                    with catch_up: true.

HOSTING. A dedicated scheduled task ("Cora - Missed Nightly Catch-Up", 08:30 AZ,
deployment/setup-missed-nightly-catchup-task.ps1), NOT the 08:45 health check: the
health check runs inside run_hidden's kill-on-close job (no BREAKAWAY), so a
fire-and-forget child would die when it exits, and a synchronous replay would
delay the Slack report by hours (Drive Sweep is unbounded). The health check only
READS this lane's ledger (nightly_health_check.check_missed_nightly_catchup).

LEDGER. logs/nightly-catchup.jsonl (NIGHTLY_CATCHUP_LEDGER_PATH; distinct from
MISSED_CATCHUP_LEDGER_PATH, which belongs to the Slack missed-message lane): one
`plan` row per --apply run (every decision) + one `task` row per replay.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SET_PATH = _REPO_ROOT / "data" / "maps" / "nightly-catchup-set.yaml"
DEFAULT_LEDGER_PATH = _REPO_ROOT / "logs" / "nightly-catchup.jsonl"
DEFAULT_TASK_LOG_DIR = _REPO_ROOT / "logs" / "tasks"

AZ = timezone(timedelta(hours=-7))   # Arizona has no DST -- a fixed offset is exact
TASK_NAME = "Cora - Missed Nightly Catch-Up"
EARLY_MIN = 10          # a fire this many minutes BEFORE the trigger still counts
#: the lane's single daily fire (deployment/setup-missed-nightly-catchup-task.ps1 $FireAt).
#: load_set() REFUSES a set whose deadline (trigger + grace) is not strictly before it:
#: such a task reads skipped_not_due at the fire every day and is never replayed
#: (D-051 review B-4: the 06:05/06:10 candidates at the 150-min default grace).
LANE_FIRE_AZ = "08:30"
#: the registered ExecutionTimeLimit (4h) the replay loop budgets against; when the
#: budget is spent the remaining `run` decisions are ledgered `not-started`, never
#: left to the scheduler's kill mid-replay with no row at all (D-051 review B-5).
LANE_BUDGET_MIN = 240
#: a spawn with less than this left in the budget is not-started (a task killed
#: seconds after launch is worse than one honestly left for tomorrow's health check)
MIN_SPAWN_BUDGET_S = 60

ACTIONS: tuple[str, ...] = (
    "fired", "skipped_not_due", "skipped_window", "skipped_already_ran", "cannot_check",
    "skipped_disabled", "skipped_disabled_in_set", "skipped_imminent", "run", "not-started",
)
#: actions the health check treats as "the night was not clean". An ENABLED task
#: whose decision is skipped_window / skipped_not_due / skipped_disabled is NOT
#: verified (nothing proved it fired, nothing replayed it) -- the 9/9 shape one step
#: longer (a host down until noon: every task skipped_window, report 'ok'). Only
#: fired and skipped_already_ran (this lane replayed it earlier today) are clean.
ATTENTION_ACTIONS: frozenset[str] = frozenset({
    "run", "cannot_check", "skipped_imminent",
    "skipped_window", "skipped_not_due", "skipped_disabled",
})
#: the subset of ATTENTION_ACTIONS that means "no evidence either way" (rendered
#: '(not verified)' by the health check)
UNVERIFIED_ACTIONS: frozenset[str] = frozenset({"skipped_window", "skipped_not_due", "skipped_disabled", "cannot_check"})
#: plan decisions that count as clean for an enabled task
VERIFIED_ACTIONS: frozenset[str] = frozenset({"fired", "skipped_already_ran"})
#: `task`-row actions where a child process WAS spawned (or the spawn was attempted):
#: these -- and only these -- make a later run of the lane read skipped_already_ran
#: and count as a replay in the Monday digest.
SPAWNED_ACTIONS: frozenset[str] = frozenset({"ran", "error", "timeout"})
#: `task`-row actions written by the replay loop's per-spawn re-check INSTEAD of a
#: spawn (the plan said `run`; the moment of the spawn said otherwise)
DEFERRED_ACTIONS: frozenset[str] = frozenset({"skipped_window", "skipped_imminent", "cannot_check", "not-started"})

# run_hidden's header: "=== run_hidden <slug> | <UTC> UTC | <AZ> AZ ===" (deployment/run_hidden.py _header)
_HEADER_RE = re.compile(
    r"^=== run_hidden (?P<slug>\S+) \| (?P<utc>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC \| "
    r"(?P<az>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) AZ ===")
# Get-TaskSlug (deployment/_task-action.ps1): a RUN of unsafe characters -> ONE dash,
# then any remaining dash run -> one dash. Both patterns are single-level (linear).
_UNSAFE_RUN_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DASH_RUN_RE = re.compile(r"-{2,}")
# the launcher's own `--name <slug>` inside a registered action's Arguments
_ACTION_NAME_RE = re.compile(r"--name(?:=|\s+)(?P<slug>\S+)")


def sanitize_slug(raw: str) -> str:
    """The slug the ESTATE registers: a faithful port of _task-action.ps1 Get-TaskSlug
    (the function that writes `--name <slug>` into every wrapped action), NOT of
    run_hidden.sanitize_slug. The two differ on runs of unsafe characters:
    Get-TaskSlug collapses them ("Cora - Drive Sweep" -> "Cora-Drive-Sweep", the
    registered --name and the real log filename), run_hidden does not
    ("Cora---Drive-Sweep", a file the estate never writes). run_hidden re-sanitizes
    the --name it receives, and every Get-TaskSlug output is a fixed point of that
    rule, so this slug IS the log filename (D-051 review B-2). Callers still prefer
    the slug parsed from the LIVE action (slug_from_action) when they have it."""
    s = _UNSAFE_RUN_RE.sub("-", raw or "")
    s = _DASH_RUN_RE.sub("-", s)
    s = s.strip("-. ")
    s = s[:80].strip("-. ")
    return s or "task"


def slug_from_action(arguments: str) -> str | None:
    """The `--name <slug>` the live registered action hands run_hidden, or None for an
    action that is not run_hidden-wrapped. Re-sanitized (idempotent for any value
    Get-TaskSlug produced) so a hand-registered odd value can never name a path
    outside logs/tasks."""
    args = str(arguments or "")
    if "run_hidden.py" not in args:
        return None
    m = _ACTION_NAME_RE.search(args)
    if not m:
        return None
    return sanitize_slug(m.group("slug"))


@dataclass(frozen=True)
class NightlyTask:
    name: str
    trigger_az: str            # "HH:MM"
    grace_min: int = 150
    max_minutes: int = 60
    enabled: bool = True
    command: str = ""          # fallback child command (the live action is preferred)
    notes: str = ""

    @property
    def slug(self) -> str:
        return sanitize_slug(self.name)

    def trigger_dt(self, day: date) -> datetime:
        hh, mm = (int(x) for x in self.trigger_az.split(":"))
        return datetime.combine(day, time(hh, mm), tzinfo=AZ)

    def deadline_dt(self, day: date) -> datetime:
        return self.trigger_dt(day) + timedelta(minutes=self.grace_min)


@dataclass(frozen=True)
class Window:
    start_az: str = "06:00"
    end_az: str = "12:00"
    default_grace_min: int = 150
    imminent_min: int = 20

    def start_dt(self, day: date) -> datetime:
        hh, mm = (int(x) for x in self.start_az.split(":"))
        return datetime.combine(day, time(hh, mm), tzinfo=AZ)

    def end_dt(self, day: date) -> datetime:
        hh, mm = (int(x) for x in self.end_az.split(":"))
        return datetime.combine(day, time(hh, mm), tzinfo=AZ)

    def contains(self, now_az: datetime, day: date) -> bool:
        return self.start_dt(day) <= now_az < self.end_dt(day)


def lane_fire_dt(day: date) -> datetime:
    hh, mm = (int(x) for x in LANE_FIRE_AZ.split(":"))
    return datetime.combine(day, time(hh, mm), tzinfo=AZ)


@dataclass
class Decision:
    task: NightlyTask
    action: str
    reason: str
    evidence: list[str] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        return {"task": self.task.name, "trigger_az": self.task.trigger_az, "action": self.action,
                "reason": self.reason, "evidence": list(self.evidence)}


# ── config ───────────────────────────────────────────────────────────────────
def load_set(path: Path | None = None) -> tuple[Window, list[NightlyTask]]:
    """The window + tasks, tasks sorted by trigger (the ruled order). Malformed rows
    are skipped with a warning; a missing file yields no tasks (the caller WARNs).
    RAISES ValueError when any task's deadline (trigger + grace) is not strictly
    before the lane's LANE_FIRE_AZ fire: that row would read skipped_not_due at
    every fire and never be replayed -- a silent hole, so the set is refused loudly
    (the lane writes no plan row and the 08:45 health check WARNs)."""
    p = Path(path) if path is not None else DEFAULT_SET_PATH
    try:
        import yaml  # lazy
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("nightly_catchup: set unreadable at %s: %s", p, exc)
        return Window(), []
    w = data.get("window") if isinstance(data, dict) else None
    w = w if isinstance(w, dict) else {}
    window = Window(
        start_az=str(w.get("start_az") or "06:00"), end_az=str(w.get("end_az") or "12:00"),
        default_grace_min=int(w.get("default_grace_min") or 150),
        imminent_min=int(w.get("imminent_min") or 20),
    )
    tasks: list[NightlyTask] = []
    for row in (data.get("tasks") if isinstance(data, dict) else None) or []:
        if not isinstance(row, dict) or not row.get("name") or not row.get("trigger_az"):
            log.warning("nightly_catchup: skipping malformed task row %r", row)
            continue
        try:
            tasks.append(NightlyTask(
                name=str(row["name"]), trigger_az=str(row["trigger_az"]),
                grace_min=int(row.get("grace_min") or window.default_grace_min),
                max_minutes=int(row.get("max_minutes") or 60),
                enabled=bool(row.get("enabled", True)), command=str(row.get("command") or ""),
                notes=str(row.get("notes") or ""),
            ))
        except (TypeError, ValueError) as exc:
            log.warning("nightly_catchup: skipping task row %r: %s", row.get("name"), exc)
    tasks.sort(key=lambda t: t.trigger_dt(date(2000, 1, 1)))
    probe = date(2000, 1, 1)
    late = [t for t in tasks if t.deadline_dt(probe) >= lane_fire_dt(probe)]
    if late:
        raise ValueError(
            "nightly_catchup: deadline (trigger + grace_min) must be strictly before the lane's "
            f"{LANE_FIRE_AZ} AZ fire, or the task reads skipped_not_due every day and is never replayed: "
            + ", ".join(f"{t.name} ({t.trigger_az} + {t.grace_min}m = {t.deadline_dt(probe).strftime('%H:%M')})"
                        for t in late))
    return window, tasks


# ── evidence ─────────────────────────────────────────────────────────────────
def header_stamps(log_dir: Path, slug: str, day: date) -> list[datetime]:
    """AZ-aware stamps of every run_hidden header for *slug* whose AZ DATE is *day*.
    Files are UTC-dated (AZ = UTC-7), so the day's fires may sit in the same or the
    next UTC file; both neighbours are scanned and the header's OWN AZ stamp is the
    key. Never enumerates the directory (test-probe leaks live there)."""
    out: list[datetime] = []
    for d in (day - timedelta(days=1), day, day + timedelta(days=1)):
        f = Path(log_dir) / f"{slug}-{d.isoformat()}.log"
        try:
            if not f.exists():
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _HEADER_RE.match(line.strip())
            if not m or m.group("slug") != slug:
                continue
            try:
                az = datetime.strptime(m.group("az"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=AZ)
            except ValueError:
                continue
            if az.date() == day:
                out.append(az)
    return sorted(out)


def marker_stamps(rows: list[dict], task_name: str, day: date) -> list[datetime]:
    """AZ-aware stamps of run markers for *task_name* whose AZ date is *day*."""
    out: list[datetime] = []
    for r in rows or []:
        if str(r.get("task") or "") != task_name:
            continue
        try:
            ts = datetime.fromisoformat(str(r.get("ts") or ""))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        az = ts.astimezone(AZ)
        if az.date() == day:
            out.append(az)
    return sorted(out)


def fired_in_window(stamps: list[datetime], task: NightlyTask, window: Window, day: date) -> list[datetime]:
    """The stamps that prove a fire for this window: at/after trigger - EARLY_MIN and
    before the window END (a hand re-fire at 08:01 counts; the 12:15 midday trigger
    does not)."""
    lo = task.trigger_dt(day) - timedelta(minutes=EARLY_MIN)
    hi = window.end_dt(day)
    return [s for s in stamps if lo <= s < hi]


# ── decision ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TaskState:
    """Task Scheduler's view of one task (None = could not be read)."""
    state: str | None = None          # Ready | Running | Disabled | ...
    next_run: datetime | None = None  # AZ-aware
    last_run: datetime | None = None  # AZ-aware
    slug: str | None = None           # the live action's `--name <slug>` (the real log filename)


def effective_slug(task: NightlyTask, state: TaskState | None) -> str:
    """The log slug to READ and to REPLAY under: the live registered --name when the
    scheduler read gave one, else the Get-TaskSlug-faithful rule."""
    return (state.slug if state is not None and state.slug else None) or task.slug


def decide(tasks: list[NightlyTask], *, window: Window, day: date, now_az: datetime,
           evidence: Callable[[NightlyTask], list[datetime]],
           states: dict[str, TaskState] | None,
           ledger_rows: list[dict]) -> list[Decision]:
    """One Decision per task, in trigger order. ``states`` None = the scheduler query
    itself failed -> every otherwise-runnable task is cannot_check."""
    out: list[Decision] = []
    # only a row where a child was SPAWNED counts as "already ran": a not-started /
    # skipped_window row from the replay loop's re-check must not block tomorrow's
    # (or a hand-run's) honest replay of the same window
    already = {(str(r.get("window_date")), str(r.get("task"))) for r in ledger_rows or []
               if r.get("row") == "task" and str(r.get("action") or "") not in DEFERRED_ACTIONS}
    for task in sorted(tasks, key=lambda t: t.trigger_dt(day)):
        if not task.enabled:
            out.append(Decision(task, "skipped_disabled_in_set", "enabled: false in the set (candidate)"))
            continue
        stamps = fired_in_window(evidence(task), task, window, day)
        if stamps:
            out.append(Decision(task, "fired", f"fired at {stamps[0].strftime('%H:%M')} AZ",
                                [s.isoformat(timespec="seconds") for s in stamps]))
            continue
        if now_az < task.deadline_dt(day):
            out.append(Decision(task, "skipped_not_due",
                                f"deadline {task.deadline_dt(day).strftime('%H:%M')} AZ not passed"))
            continue
        if not window.contains(now_az, day):
            out.append(Decision(task, "skipped_window",
                                f"now {now_az.strftime('%H:%M')} AZ is outside {window.start_az}-{window.end_az}"))
            continue
        if (day.isoformat(), task.name) in already:
            out.append(Decision(task, "skipped_already_ran", "this lane already replayed it for this window"))
            continue
        st = (states or {}).get(task.name) if states is not None else None
        if states is None or st is None or not st.state:
            out.append(Decision(task, "cannot_check", "Task Scheduler state unreadable -- not replaying blind"))
            continue
        if st.last_run is not None and fired_in_window([st.last_run], task, window, day):
            out.append(Decision(task, "fired", f"scheduler LastRunTime {st.last_run.strftime('%H:%M')} AZ",
                                [st.last_run.isoformat(timespec="seconds")]))
            continue
        if st.state.lower() == "running":
            out.append(Decision(task, "cannot_check", "Task Scheduler reports the task RUNNING -- never a second instance"))
            continue
        if st.state.lower() == "disabled":
            out.append(Decision(task, "skipped_disabled", "task is Disabled in Task Scheduler"))
            continue
        if st.next_run is not None and timedelta(0) <= (st.next_run - now_az) < timedelta(minutes=window.imminent_min):
            out.append(Decision(task, "skipped_imminent",
                                f"next scheduled run {st.next_run.strftime('%H:%M')} AZ is imminent"))
            continue
        out.append(Decision(task, "run", f"no fire evidence for {day.isoformat()}; deadline passed"))
    return out


def recheck_before_spawn(task: NightlyTask, *, window: Window, day: date, now_az: datetime,
                         state: TaskState | None, budget_left_s: float) -> tuple[str, str] | None:
    """The plan said `run` at 08:30; is a spawn still right NOW? Earlier replays can
    run for hours, so the window / imminent / single-instance rules are re-applied
    at each spawn, plus the lane's own time budget. None = spawn; else the
    (action, reason) of the `task` row to write INSTEAD of spawning."""
    if now_az >= window.end_dt(day):
        return ("skipped_window",
                f"now {now_az.strftime('%H:%M')} AZ is past the window end {window.end_az} (earlier replays ran long)")
    if budget_left_s < MIN_SPAWN_BUDGET_S:
        return ("not-started",
                f"lane budget ({LANE_BUDGET_MIN} min) exhausted before this spawn -- {max(0, int(budget_left_s))}s left")
    if state is not None and state.state and state.state.lower() == "running":
        return ("cannot_check", "Task Scheduler reports the task RUNNING at spawn time -- never a second instance")
    if (state is not None and state.next_run is not None
            and timedelta(0) <= (state.next_run - now_az) < timedelta(minutes=window.imminent_min)):
        return ("skipped_imminent",
                f"next scheduled run {state.next_run.strftime('%H:%M')} AZ is imminent at spawn time")
    return None


# ── ledger ───────────────────────────────────────────────────────────────────
def ledger_path() -> Path:
    return Path(os.environ.get("NIGHTLY_CATCHUP_LEDGER_PATH", "") or DEFAULT_LEDGER_PATH)


def read_ledger(path: Path | None = None) -> list[dict]:
    p = Path(path) if path else ledger_path()
    if not p.exists():
        return []
    rows: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(r, dict):
                rows.append(r)
    except OSError:
        return rows
    return rows


def append_ledger(row: dict, path: Path | None = None) -> bool:
    p = Path(path) if path else ledger_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 -- observability never raises into the lane
        log.error("NIGHTLY_CATCHUP_LEDGER_WRITE_FAILING path=%s err=%s", p, exc)
        return False


def rows_for_day(rows: list[dict], day: date) -> list[dict]:
    return [r for r in rows if str(r.get("window_date")) == day.isoformat()]


def plan_row(day: date, now_az: datetime, mode: str, decisions: list[Decision]) -> dict:
    return {"row": "plan", "ts": now_az.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "window_date": day.isoformat(), "mode": mode,
            "decisions": [d.as_row() for d in decisions],
            "counts": counts(decisions)}


def counts(decisions: list[Decision]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in decisions:
        out[d.action] = out.get(d.action, 0) + 1
    return out


def summarize_day(rows: list[dict], day: date) -> dict[str, Any]:
    """What the health check renders for one window date: the latest plan's counts +
    the replay rows. {present: bool, counts, replays: [...], deferred: [...], plan_ts}.
    `replays` = task rows where a child was spawned; `deferred` = task rows the replay
    loop wrote INSTEAD of a spawn (DEFERRED_ACTIONS) -- the digest counts only the
    former as replays, the health check WARNs on both."""
    todays = rows_for_day(rows, day)
    plans = [r for r in todays if r.get("row") == "plan"]
    task_rows = [r for r in todays if r.get("row") == "task"]
    if not plans and not task_rows:
        return {"present": False, "counts": {}, "replays": [], "deferred": [], "plan_ts": ""}
    plan = plans[-1] if plans else {}

    def _slim(r: dict) -> dict:
        return {"task": r.get("task"), "action": r.get("action"), "rc": r.get("rc"),
                "duration_s": r.get("duration_s"), "reason": r.get("reason")}

    return {"present": True, "counts": dict(plan.get("counts") or {}), "plan_ts": str(plan.get("ts") or ""),
            "mode": str(plan.get("mode") or ""),
            "decisions": list(plan.get("decisions") or []),
            "replays": [_slim(r) for r in task_rows if str(r.get("action") or "") not in DEFERRED_ACTIONS],
            "deferred": [_slim(r) for r in task_rows if str(r.get("action") or "") in DEFERRED_ACTIONS]}


def format_plan(decisions: list[Decision]) -> str:
    lines = [f"{'ACTION':<24} {'TRIGGER':<8} TASK -- reason"]
    for d in decisions:
        lines.append(f"{d.action:<24} {d.task.trigger_az:<8} {d.task.name} -- {d.reason}")
    c = counts(decisions)
    lines.append("counts: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    return "\n".join(lines)


# ── live action parsing ──────────────────────────────────────────────────────
def child_command_from_action(execute: str, arguments: str) -> str:
    """The VERBATIM child command behind a registered action: for a run_hidden-wrapped
    action everything after the first ' -- '; else Execute + Arguments."""
    args = str(arguments or "")
    if "run_hidden.py" in args and " -- " in args:
        return args.split(" -- ", 1)[1].strip()
    return (str(execute or "").strip() + " " + args).strip()


def launcher_command_line(pythonw: Path, launcher: Path, slug: str, child: str) -> str:
    """The raw command line run_hidden expects (GetCommandLineW split at the first
    ' -- ' sentinel), built as a STRING so the child survives byte-for-byte."""
    return f'"{pythonw}" "{launcher}" --name {slug} -- {child}'
