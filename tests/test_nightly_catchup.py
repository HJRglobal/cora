"""Missed-start catch-up for the nightly ingest set (Code #13 slice 2, cq-fb50c9e6c911).

Contract under test:
  * the slug rule is BYTE-FOR-BYTE the launcher's (a divergence would make the lane
    read a log file the launcher never writes);
  * the shipped set loads sorted by trigger; the twelve kickoff-named tasks are
    enabled, the four 9/9-lost candidates are `enabled: false`;
  * evidence: a run_hidden header inside the window counts; the midday header does
    not; a hand re-fire at 08:01 counts; a run marker counts; files are UTC-dated but
    the header's own AZ stamp is the key;
  * REPLAY FIXTURE (D-256): the 9/9 shape -- only the 12:15/12:20/12:30 midday headers
    for mirror/static/capture, no files for the other tasks, one session-capture
    marker at 12:38 AZ -- selects EXACTLY the twelve enabled tasks, in trigger order;
    the 9/10 shape (every header at its trigger) selects nothing;
  * a task Task Scheduler reports RUNNING = cannot_check, never run; an unreadable
    scheduler = cannot_check; Disabled = skipped; an imminent next trigger = skipped;
    a task already replayed for this window (own ledger) = skipped; outside
    06:00-12:00 AZ = skipped; before the deadline = not due;
  * the script: past-day --apply is REFUSED; dry-run writes nothing and spawns
    nothing; --apply spawns each `run` task through run_hidden with the LIVE child
    command verbatim (one STRING command line), in order, writes a plan row + a
    task row each + ONE run marker with catch_up: true; the scheduler output parser
    strips the run_hidden prefix.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from cora import nightly_catchup as nc
from cora import run_marker

_REPO = Path(__file__).resolve().parents[1]
_REAL_SET = _REPO / "data" / "maps" / "nightly-catchup-set.yaml"
_LAUNCHER = _REPO / "deployment" / "run_hidden.py"
_SCRIPT = _REPO / "scripts" / "check_missed_nightly.py"

DAY_0909 = date(2026, 9, 9)
DAY_0910 = date(2026, 9, 10)
DAY_0911 = date(2026, 9, 11)
ENABLED_TWELVE = [
    "cowork-cora-kb-sync-slack", "cowork-cora-kb-sync-gmail", "cowork-cora-kb-sync-asana",
    "cowork-cora-kb-sync-fireflies", "cowork-cora-claude-mirror", "cowork-cora-kb-sync-static",
    "cowork-cora-kb-sync-drive", "Cora - LEX Dump Folder Sync", "cowork-cora-kb-sync-notion",
    "cowork-cora-session-capture", "Cora - Drive Materialization", "Cora - Drive Sweep",
]
CANDIDATES_FOUR = ["cowork-cora-qbo-token-refresh", "cowork-cora-reconciliation",
                   "cowork-cora-info-for-cora-sweep", "cowork-cora-gap-autofill"]
# RULED 2026-09-19 (ask 9.7, Harrison commit f65b7fc): all four candidates ENABLED.
# Under T1 (no --apply) this only widens what the dry-run REPORTS. The shipped set
# is therefore sixteen enabled, in trigger order (the four interleave by trigger).
ENABLED_SIXTEEN = [
    "cowork-cora-kb-sync-slack", "cowork-cora-qbo-token-refresh", "cowork-cora-kb-sync-gmail",
    "cowork-cora-kb-sync-asana", "cowork-cora-kb-sync-fireflies", "cowork-cora-claude-mirror",
    "cowork-cora-kb-sync-static", "cowork-cora-kb-sync-drive", "Cora - LEX Dump Folder Sync",
    "cowork-cora-kb-sync-notion", "cowork-cora-session-capture", "cowork-cora-reconciliation",
    "Cora - Drive Materialization", "Cora - Drive Sweep", "cowork-cora-info-for-cora-sweep",
    "cowork-cora-gap-autofill",
]
assert sorted(ENABLED_SIXTEEN) == sorted(ENABLED_TWELVE + CANDIDATES_FOUR)


def _az(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=nc.AZ)


def _header(slug: str, az: datetime) -> str:
    utc = az.astimezone(timezone.utc)
    return (f"\n=== run_hidden {slug} | {utc.strftime('%Y-%m-%d %H:%M:%S')} UTC | "
            f"{az.strftime('%Y-%m-%d %H:%M:%S')} AZ ===\nCMD: x\nEXIT: 0\n")


def _write_header(log_dir: Path, slug: str, az: datetime) -> None:
    utc_day = az.astimezone(timezone.utc).date().isoformat()
    f = log_dir / f"{slug}-{utc_day}.log"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(_header(slug, az))


def _load_launcher():
    spec = importlib.util.spec_from_file_location("cora_run_hidden_for_catchup", _LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_script():
    spec = importlib.util.spec_from_file_location("check_missed_nightly_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_missed_nightly_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    p = tmp_path / "nightly-catchup.jsonl"
    monkeypatch.setenv("NIGHTLY_CATCHUP_LEDGER_PATH", str(p))
    return p


# ── slug + set ────────────────────────────────────────────────────────────────

#: the --name values the LIVE registered actions carry for the three space-named tasks
#: (Get-ScheduledTask read 2026-09-19, D-051 review B-2) -- the real log filenames
LIVE_SLUGS = {
    "Cora - Drive Sweep": "Cora-Drive-Sweep",
    "Cora - LEX Dump Folder Sync": "Cora-LEX-Dump-Folder-Sync",
    "Cora - Drive Materialization": "Cora-Drive-Materialization",
    "Cora - Daily Synthesis (F3E)": "Cora-Daily-Synthesis-F3E",     # the _task-action.ps1 docstring example
}


class TestSlugAndSet:
    @pytest.mark.parametrize("name,expected", sorted(LIVE_SLUGS.items()))
    def test_slug_rule_is_get_task_slug_not_run_hidden(self, name, expected):
        """B-2 regression: the old rule copied run_hidden.sanitize_slug (no dash-run
        collapse) and produced 'Cora---Drive-Sweep', a file the estate never writes;
        the registered action says `--name Cora-Drive-Sweep`."""
        assert nc.sanitize_slug(name) == expected
        rh = _load_launcher()
        assert rh.sanitize_slug(name) != expected                     # the old oracle really differs on these
        # the launcher re-sanitizes the --name it receives: our slug must be its fixed point
        assert rh.sanitize_slug(expected) == expected

    @pytest.mark.parametrize("name", ENABLED_TWELVE + CANDIDATES_FOUR + [
        "Cora - Daily Synthesis (F3E)", "", "..weird..", "a" * 100, "Cora - Missed Nightly Catch-Up",
        "A - " + "b" * 100, ("y" * 79) + " zzz", "...", "..\\..\\evil"])
    def test_slug_is_a_fixed_point_of_the_launcher_and_never_has_a_dash_run(self, name):
        rh = _load_launcher()
        s = nc.sanitize_slug(name)
        assert rh.sanitize_slug(s) == s and "--" not in s and s == nc.sanitize_slug(s)
        assert len(s) <= 80 and s and not s.startswith(("-", ".")) and not s.endswith(("-", "."))

    @pytest.mark.skipif(os.name != "nt", reason="PowerShell helper")
    def test_slug_rule_matches_get_task_slug_for_real(self):
        """Derive the oracle by CALLING Get-TaskSlug (the function that writes every
        registered --name), not by re-typing it."""
        helper = _REPO / "deployment" / "_task-action.ps1"
        names = ENABLED_TWELVE + CANDIDATES_FOUR + ["Cora - Daily Synthesis (F3E)", "Cora - Missed Nightly Catch-Up",
                                                    "A - " + "b" * 100, ("y" * 79) + " zzz", "..\\..\\evil", "..."]
        quoted = ",".join("'" + n.replace("'", "''") + "'" for n in names)
        ps = ('. "%s"\n$names = @(%s)\nforeach ($n in $names) { Write-Output (Get-TaskSlug $n) }'
              % (helper, quoted))
        proc = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                              capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        ps_slugs = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        assert len(ps_slugs) == len(names)
        for name, slug in zip(names, ps_slugs):
            assert nc.sanitize_slug(name) == slug, f"{name!r}: python {nc.sanitize_slug(name)!r} != Get-TaskSlug {slug!r}"

    def test_slug_regexes_are_linear_growth_shape(self):
        """The two new patterns (unsafe-run, dash-run) and the --name parser are
        single-level: 200x the input must cost well under 100x the time."""
        import time as _t
        small = ("- " * 500) + ("x" * 500) + ("--" * 500)
        big = small * 200
        t0 = _t.perf_counter(); nc.sanitize_slug(small); t_small = _t.perf_counter() - t0
        t0 = _t.perf_counter(); nc.sanitize_slug(big); t_big = _t.perf_counter() - t0
        assert t_big < 1.0, f"200x input took {t_big:.3f}s (small {t_small:.5f}s)"
        args = "run_hidden.py " + ("--nam " * 20000) + "--name " + ("s" * 20000) + " -- child"
        t0 = _t.perf_counter(); got = nc.slug_from_action(args); t_parse = _t.perf_counter() - t0
        assert t_parse < 1.0 and got == "s" * 80

    def test_slug_from_action_reads_the_live_name_and_prefers_it(self):
        args = ('"C:\\x\\deployment\\run_hidden.py" --name Cora-Drive-Sweep -- '
                '"C:\\x\\.venv\\Scripts\\python.exe" "C:\\x\\scripts\\run_drive_sweep.py" --with-slack')
        assert nc.slug_from_action(args) == "Cora-Drive-Sweep"
        assert nc.slug_from_action('"C:\\x\\scripts\\a.py" --with-kb') is None       # not wrapped
        assert nc.slug_from_action("run_hidden.py --name=odd/..\\name -- c") == "odd-..-name"  # re-sanitized (no traversal)
        t = nc.NightlyTask(name="Cora - Drive Sweep", trigger_az="06:00")
        assert nc.effective_slug(t, nc.TaskState(state="Ready", slug="Hand-Registered")) == "Hand-Registered"
        assert nc.effective_slug(t, nc.TaskState(state="Ready")) == "Cora-Drive-Sweep"
        assert nc.effective_slug(t, None) == "Cora-Drive-Sweep"

    def test_shipped_set_loads_sorted_with_all_sixteen_enabled_as_ruled(self):
        """Re-pinned 2026-09-22 (Code #14): the four candidates were enabled by the
        9.7 ruling (f65b7fc); this test pinned the pre-ruling twelve-plus-four."""
        window, tasks = nc.load_set(_REAL_SET)
        assert window.start_az == "06:00" and window.end_az == "12:00"
        names = [t.name for t in tasks]
        assert [t.name for t in tasks if t.enabled] == ENABLED_SIXTEEN
        assert [t.name for t in tasks if not t.enabled] == []
        triggers = [t.trigger_dt(DAY_0909) for t in tasks]
        assert triggers == sorted(triggers)
        assert all(t.command for t in tasks) and all(t.max_minutes > 0 for t in tasks)
        assert "cowork-cora-session-capture" in names and names.index("cowork-cora-kb-sync-static") < names.index("cowork-cora-session-capture")

    def test_missing_or_malformed_set_yields_no_tasks(self, tmp_path):
        assert nc.load_set(tmp_path / "nope.yaml")[1] == []
        bad = tmp_path / "bad.yaml"
        bad.write_text("tasks:\n  - name: x\n  - trigger_az: '04:00'\n  - name: ok\n    trigger_az: '04:00'\n", encoding="utf-8")
        _, tasks = nc.load_set(bad)
        assert [t.name for t in tasks] == ["ok"]


# ── evidence ─────────────────────────────────────────────────────────────────

class TestEvidence:
    def test_header_inside_window_counts_midday_does_not(self, tmp_path):
        slug = "cowork-cora-kb-sync-static"
        _write_header(tmp_path, slug, _az(DAY_0909, 12, 20, 1))     # midday only (the 9/9 shape)
        task = nc.NightlyTask(name="cowork-cora-kb-sync-static", trigger_az="04:00")
        stamps = nc.header_stamps(tmp_path, slug, DAY_0909)
        assert len(stamps) == 1
        assert nc.fired_in_window(stamps, task, nc.Window(), DAY_0909) == []
        _write_header(tmp_path, slug, _az(DAY_0910, 4, 0, 1))
        stamps10 = nc.header_stamps(tmp_path, slug, DAY_0910)
        assert len(nc.fired_in_window(stamps10, task, nc.Window(), DAY_0910)) == 1

    def test_hand_refire_at_0801_counts_as_fired(self, tmp_path):
        slug = "cowork-cora-claude-mirror"
        _write_header(tmp_path, slug, _az(DAY_0910, 8, 1, 30))
        task = nc.NightlyTask(name="cowork-cora-claude-mirror", trigger_az="03:45")
        assert len(nc.fired_in_window(nc.header_stamps(tmp_path, slug, DAY_0910), task, nc.Window(), DAY_0910)) == 1

    def test_header_of_another_slug_in_the_same_file_is_ignored(self, tmp_path):
        f = tmp_path / "cowork-cora-kb-sync-static-2026-09-10.log"
        f.write_text(_header("cowork-cora-kb-sync-slack", _az(DAY_0910, 4, 0)), encoding="utf-8")
        assert nc.header_stamps(tmp_path, "cowork-cora-kb-sync-static", DAY_0910) == []

    def test_utc_dated_file_neighbour_is_scanned_by_az_stamp(self, tmp_path):
        """A 20:30 AZ fire lands in the NEXT UTC day's file; the AZ stamp keys the day."""
        slug = "x-task"
        _write_header(tmp_path, slug, _az(DAY_0909, 20, 30))
        assert (tmp_path / f"{slug}-2026-09-10.log").exists()
        assert len(nc.header_stamps(tmp_path, slug, DAY_0909)) == 1
        assert nc.header_stamps(tmp_path, slug, DAY_0910) == []

    def test_marker_stamps_key_on_the_az_date(self):
        rows = [{"task": "cowork-cora-session-capture", "ts": "2026-09-09T19:38:55+00:00"},
                {"task": "cowork-cora-session-capture", "ts": "2026-09-10T12:15:02+00:00"},
                {"task": "other", "ts": "2026-09-09T12:15:02+00:00"}]
        s = nc.marker_stamps(rows, "cowork-cora-session-capture", DAY_0909)
        assert [x.strftime("%H:%M") for x in s] == ["12:38"]
        task = nc.NightlyTask(name="cowork-cora-session-capture", trigger_az="05:15")
        assert nc.fired_in_window(s, task, nc.Window(), DAY_0909) == []          # midday marker: not the morning fire
        s10 = nc.marker_stamps(rows, "cowork-cora-session-capture", DAY_0910)
        assert len(nc.fired_in_window(s10, task, nc.Window(), DAY_0910)) == 1  # 05:15 AZ marker


# ── the 9/9 replay fixture ────────────────────────────────────────────────────

def _shape_0909(log_dir: Path) -> None:
    _write_header(log_dir, "cowork-cora-claude-mirror", _az(DAY_0909, 12, 15, 1))
    _write_header(log_dir, "cowork-cora-kb-sync-static", _az(DAY_0909, 12, 20, 1))
    _write_header(log_dir, "cowork-cora-session-capture", _az(DAY_0909, 12, 30, 2))


def _shape_0910(log_dir: Path, tasks) -> None:
    for t in tasks:
        _write_header(log_dir, t.slug, t.trigger_dt(DAY_0910) + timedelta(seconds=1))
    _write_header(log_dir, "cowork-cora-claude-mirror", _az(DAY_0910, 8, 1))   # the hand re-fire too


def _ready(tasks):
    return {t.name: nc.TaskState(state="Ready", next_run=_az(DAY_0910, 3, 0)) for t in tasks}


def _decide(tasks, window, day, now_az, log_dir, markers, states, ledger_rows=()):
    def evidence(task):
        return nc.header_stamps(log_dir, task.slug, day) + nc.marker_stamps(markers, task.name, day)
    return nc.decide(tasks, window=window, day=day, now_az=now_az, evidence=evidence,
                     states=states, ledger_rows=list(ledger_rows))


class TestReplayFixture:
    def test_0909_selects_exactly_the_twelve_in_trigger_order(self, tmp_path):
        window, tasks = nc.load_set(_REAL_SET)
        _shape_0909(tmp_path)
        markers = [{"task": "cowork-cora-session-capture", "ts": "2026-09-09T19:38:55+00:00", "ok": True},
                   {"task": "cowork-cora-meeting-capture-audit", "ts": "2026-09-09T14:22:19+00:00", "ok": True}]
        decisions = _decide(tasks, window, DAY_0909, _az(DAY_0909, 8, 45), tmp_path, markers, _ready(tasks))
        runs = [d.task.name for d in decisions if d.action == "run"]
        # With the ruled set every enabled task with no in-window evidence is a run,
        # the four ex-candidates included (their 9/9 logs are absent from the shape).
        assert runs == ENABLED_SIXTEEN
        assert {d.task.name for d in decisions if d.action == "skipped_disabled_in_set"} == set()
        assert not [d for d in decisions if d.action == "fired"]

    def test_0910_selects_nothing(self, tmp_path):
        window, tasks = nc.load_set(_REAL_SET)
        _shape_0910(tmp_path, tasks)
        decisions = _decide(tasks, window, DAY_0910, _az(DAY_0910, 8, 45), tmp_path, [], _ready(tasks))
        assert [d.task.name for d in decisions if d.action == "run"] == []
        assert sorted(d.task.name for d in decisions if d.action == "fired") == sorted(ENABLED_SIXTEEN)

    def test_0910_with_only_the_hand_refire_still_reads_fired(self, tmp_path):
        window, tasks = nc.load_set(_REAL_SET)
        _write_header(tmp_path, "cowork-cora-claude-mirror", _az(DAY_0910, 8, 1, 30))
        decisions = _decide(tasks, window, DAY_0910, _az(DAY_0910, 8, 45), tmp_path, [], _ready(tasks))
        by = {d.task.name: d for d in decisions}
        assert by["cowork-cora-claude-mirror"].action == "fired"
        assert by["cowork-cora-kb-sync-static"].action == "run"


class TestDecisionRules:
    def _one(self, **over):
        t = nc.NightlyTask(name="cowork-cora-kb-sync-static", trigger_az="04:00", **over)
        return t

    def test_running_is_cannot_check_never_run(self, tmp_path):
        t = self._one()
        d = _decide([t], nc.Window(), DAY_0909, _az(DAY_0909, 8, 45), tmp_path, [],
                    {t.name: nc.TaskState(state="Running")})[0]
        assert d.action == "cannot_check" and "RUNNING" in d.reason

    def test_unreadable_scheduler_is_cannot_check(self, tmp_path):
        t = self._one()
        assert _decide([t], nc.Window(), DAY_0909, _az(DAY_0909, 8, 45), tmp_path, [], None)[0].action == "cannot_check"
        assert _decide([t], nc.Window(), DAY_0909, _az(DAY_0909, 8, 45), tmp_path, [], {})[0].action == "cannot_check"
        assert _decide([t], nc.Window(), DAY_0909, _az(DAY_0909, 8, 45), tmp_path, [],
                       {t.name: nc.TaskState(state=None)})[0].action == "cannot_check"

    def test_disabled_and_imminent_skip(self, tmp_path):
        t = self._one()
        assert _decide([t], nc.Window(), DAY_0909, _az(DAY_0909, 8, 45), tmp_path, [],
                       {t.name: nc.TaskState(state="Disabled")})[0].action == "skipped_disabled"
        now = _az(DAY_0909, 11, 50)
        d = _decide([t], nc.Window(), DAY_0909, now, tmp_path, [],
                    {t.name: nc.TaskState(state="Ready", next_run=now + timedelta(minutes=10))})[0]
        assert d.action == "skipped_imminent"

    def test_scheduler_last_run_inside_the_window_reads_as_fired(self, tmp_path):
        t = self._one()
        d = _decide([t], nc.Window(), DAY_0909, _az(DAY_0909, 8, 45), tmp_path, [],
                    {t.name: nc.TaskState(state="Ready", last_run=_az(DAY_0909, 4, 0, 5))})[0]
        assert d.action == "fired" and "LastRunTime" in d.reason

    def test_window_and_deadline_rules(self, tmp_path):
        t = self._one()
        assert _decide([t], nc.Window(), DAY_0909, _az(DAY_0909, 13, 0), tmp_path, [], _ready([t]))[0].action == "skipped_window"
        assert _decide([t], nc.Window(), DAY_0909, _az(DAY_0909, 5, 0), tmp_path, [], _ready([t]))[0].action == "skipped_not_due"
        assert _decide([t], nc.Window(), DAY_0909, _az(DAY_0909, 6, 31), tmp_path, [], _ready([t]))[0].action == "run"

    def test_never_twice_for_one_window(self, tmp_path):
        t = self._one()
        rows = [{"row": "task", "window_date": "2026-09-09", "task": t.name, "action": "ran"}]
        assert _decide([t], nc.Window(), DAY_0909, _az(DAY_0909, 8, 45), tmp_path, [], _ready([t]), rows)[0].action == "skipped_already_ran"
        rows10 = [{"row": "task", "window_date": "2026-09-10", "task": t.name, "action": "ran"}]
        assert _decide([t], nc.Window(), DAY_0909, _az(DAY_0909, 8, 45), tmp_path, [], _ready([t]), rows10)[0].action == "run"

    def test_disabled_in_set_never_runs_even_when_missing(self, tmp_path):
        t = self._one(enabled=False)
        assert _decide([t], nc.Window(), DAY_0909, _az(DAY_0909, 8, 45), tmp_path, [], _ready([t]))[0].action == "skipped_disabled_in_set"


# ── ledger + helpers ─────────────────────────────────────────────────────────

class TestLedgerAndHelpers:
    def test_ledger_roundtrip_and_day_summary(self, ledger):
        t = nc.NightlyTask(name="a", trigger_az="04:00")
        decisions = [nc.Decision(t, "run", "r")]
        assert nc.append_ledger(nc.plan_row(DAY_0909, _az(DAY_0909, 8, 30), "apply", decisions))
        assert nc.append_ledger({"row": "task", "window_date": "2026-09-09", "task": "a", "action": "ran", "rc": 0, "duration_s": 3.2})
        rows = nc.read_ledger()
        s = nc.summarize_day(rows, DAY_0909)
        assert s["present"] and s["counts"] == {"run": 1} and s["replays"][0]["action"] == "ran"
        assert nc.summarize_day(rows, DAY_0910)["present"] is False

    def test_child_command_parsing(self):
        wrapped = ('C:\\x\\deployment\\run_hidden.py --name cowork-cora-kb-sync-static -- '
                   'cmd.exe /c cd /d "C:\\x" & "C:\\x\\.venv\\Scripts\\python.exe" "C:\\x\\scripts\\incremental_sync_static.py"')
        child = nc.child_command_from_action("C:\\x\\.venv\\Scripts\\pythonw.exe", wrapped)
        assert child.startswith('cmd.exe /c cd /d "C:\\x" &') and "run_hidden" not in child
        plain = nc.child_command_from_action("C:\\x\\.venv\\Scripts\\python.exe", '"C:\\x\\scripts\\a.py" --with-kb')
        assert plain == 'C:\\x\\.venv\\Scripts\\python.exe "C:\\x\\scripts\\a.py" --with-kb'

    def test_launcher_command_line_keeps_the_child_verbatim(self):
        child = 'cmd.exe /c cd /d "C:\\x" & "C:\\x\\py.exe" "C:\\x\\s.py"'
        line = nc.launcher_command_line(Path("C:/x/pythonw.exe"), Path("C:/x/run_hidden.py"), "slug", child)
        assert line.endswith(" -- " + child) and "--name slug" in line

    def test_format_plan_lists_every_decision(self):
        t = nc.NightlyTask(name="a", trigger_az="04:00")
        text = nc.format_plan([nc.Decision(t, "run", "r"), nc.Decision(t, "fired", "f")])
        assert "run" in text and "fired" in text and "counts: fired=1, run=1" in text


# ── the script ────────────────────────────────────────────────────────────────

class TestScript:
    def test_parse_task_states_strips_the_run_hidden_prefix(self):
        mod = _load_script()
        out = ("cowork-cora-kb-sync-static|Ready|2026-09-20T04:00:00|2026-09-19T04:00:01|"
               "C:\\x\\pythonw.exe|C:\\x\\deployment\\run_hidden.py --name cowork-cora-kb-sync-static -- "
               "cmd.exe /c cd /d \"C:\\x\" & \"C:\\x\\python.exe\" \"C:\\x\\s.py\"\n"
               "Cora - Drive Sweep|Running||2026-09-19T06:00:01|C:\\x\\python.exe|\"C:\\x\\d.py\" --with-slack\n"
               "ghost|MISSING||||\n")
        states, actions = mod.parse_task_states(out)
        assert states["cowork-cora-kb-sync-static"].state == "Ready"
        assert states["cowork-cora-kb-sync-static"].next_run == datetime(2026, 9, 20, 4, 0, tzinfo=nc.AZ)
        assert actions["cowork-cora-kb-sync-static"].startswith("cmd.exe /c cd /d")
        assert states["Cora - Drive Sweep"].state == "Running" and states["Cora - Drive Sweep"].next_run is None
        assert actions["Cora - Drive Sweep"] == 'C:\\x\\python.exe "C:\\x\\d.py" --with-slack'
        assert states["ghost"].state is None

    def test_past_day_apply_is_refused(self, ledger, tmp_path):
        mod = _load_script()
        rc = mod.main(["--apply", "--day", "2026-09-09", "--log-dir", str(tmp_path)],
                      now_az=_az(DAY_0910, 8, 30), states_reader=lambda names: ({}, {}))
        assert rc == 2 and not ledger.exists()

    def test_dry_run_replays_0909_and_writes_nothing(self, ledger, tmp_path, capsys, monkeypatch):
        mod = _load_script()
        _shape_0909(tmp_path)
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "task-runs.jsonl"))
        spawned = []
        rc = mod.main(["--day", "2026-09-09", "--log-dir", str(tmp_path), "--no-scheduler"],
                      now_az=_az(DAY_0909, 8, 45), popen=lambda *a, **k: spawned.append(a) or None)
        out = capsys.readouterr().out
        assert rc == 0 and spawned == [] and not ledger.exists()
        assert out.count("run ") >= 12 and "Cora - Drive Sweep" in out

    def test_apply_spawns_in_order_verbatim_and_ledgers_each(self, ledger, tmp_path, monkeypatch):
        mod = _load_script()
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "task-runs.jsonl"))
        _write_header(tmp_path, "cowork-cora-kb-sync-slack", _az(DAY_0910, 2, 0, 1))   # slack fired; others missing
        set_yaml = tmp_path / "set.yaml"
        set_yaml.write_text(
            "tasks:\n"
            "  - name: cowork-cora-kb-sync-slack\n    trigger_az: '02:00'\n    command: slack-fallback\n"
            "  - name: cowork-cora-kb-sync-static\n    trigger_az: '04:00'\n    command: static-fallback\n"
            "  - name: cowork-cora-session-capture\n    trigger_az: '05:15'\n    command: capture-fallback\n",
            encoding="utf-8")
        live_child = 'cmd.exe /c cd /d "C:\\x" & "C:\\x\\python.exe" "C:\\x\\incremental_sync_static.py"'

        def reader(names):
            return ({n: nc.TaskState(state="Ready", next_run=_az(DAY_0910, 12, 20)) for n in names},
                    {"cowork-cora-kb-sync-static": live_child})

        calls = []

        class _Proc:
            def wait(self, timeout=None):
                return 0

        def popen(cmdline, **kw):
            calls.append((cmdline, kw))
            return _Proc()

        rc = mod.main(["--apply", "--set", str(set_yaml), "--log-dir", str(tmp_path)],
                      now_az=_az(DAY_0910, 8, 30), states_reader=reader, popen=popen)
        assert rc == 0
        # order: static (04:00) then capture (05:15); slack fired -> not replayed
        assert len(calls) == 2
        assert calls[0][0].endswith(" -- " + live_child) and "--name cowork-cora-kb-sync-static" in calls[0][0]
        assert calls[1][0].endswith(" -- capture-fallback")          # no live action -> yaml fallback
        assert all(isinstance(c[0], str) for c in calls)            # ONE string, never a re-quoted list
        rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()]
        assert rows[0]["row"] == "plan" and rows[0]["counts"] == {"fired": 1, "run": 2}
        assert [r["task"] for r in rows if r["row"] == "task"] == ["cowork-cora-kb-sync-static", "cowork-cora-session-capture"]
        assert all(r["action"] == "ran" and r["rc"] == 0 for r in rows if r["row"] == "task")
        markers = run_marker.read_markers()
        assert len(markers) == 1 and markers[0]["task"] == nc.TASK_NAME
        assert markers[0]["catch_up"] is True and markers[0]["outputs"] == 2 and markers[0]["window"] == "2026-09-10"
        # a second --apply for the same window replays NOTHING (never twice)
        calls.clear()
        rc2 = mod.main(["--apply", "--set", str(set_yaml), "--log-dir", str(tmp_path)],
                       now_az=_az(DAY_0910, 9, 0), states_reader=reader, popen=popen)
        assert rc2 == 0 and calls == []

    def test_timeout_is_recorded_and_the_child_killed(self, ledger, tmp_path, monkeypatch):
        import subprocess
        mod = _load_script()
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "task-runs.jsonl"))
        set_yaml = tmp_path / "set.yaml"
        set_yaml.write_text("tasks:\n  - name: t\n    trigger_az: '04:00'\n    command: c\n    max_minutes: 1\n", encoding="utf-8")
        killed = []

        class _Proc:
            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("c", timeout)

            def kill(self):
                killed.append(True)

        rc = mod.main(["--apply", "--set", str(set_yaml), "--log-dir", str(tmp_path)],
                      now_az=_az(DAY_0910, 8, 30),
                      states_reader=lambda names: ({"t": nc.TaskState(state="Ready")}, {}),
                      popen=lambda *a, **k: _Proc())
        assert rc == 1 and killed == [True]
        rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()]
        assert [r["action"] for r in rows if r["row"] == "task"] == ["timeout"]

    def test_script_bootstrap_and_no_model(self):
        src = _SCRIPT.read_text(encoding="utf-8")
        assert src.index("from cora import nightly_catchup") > src.index('sys.path.insert(0, str(_REPO_ROOT / "src"))')
        assert "anthropic" not in src.lower() and "claude_client" not in src
        assert "CREATE_NO_WINDOW" in src

    def test_setup_ps1_is_ascii_and_registers_the_lane(self):
        ps1 = (_REPO / "deployment" / "setup-missed-nightly-catchup-task.ps1").read_bytes()
        assert all(b < 128 for b in ps1), "D-016: ASCII-only"
        text = ps1.decode("ascii")
        assert "Cora - Missed Nightly Catch-Up" in text and "--apply" in text and "New-WrappedTaskAction" in text
        assert '"08:30"' in text and ".venv\\Scripts\\python.exe" in text


# ── the readers: 08:45 health check + Monday digest ──────────────────────────

sys.path.insert(0, str(_REPO / "scripts"))
import nightly_health_check as nhc  # noqa: E402
import cora_health_report as chr_  # noqa: E402


def _plan(ledger: Path, day: date, decisions: list[tuple[str, str]], hh=8, mm=30) -> None:
    rows = [{"task": t, "trigger_az": "04:00", "action": a, "reason": "r", "evidence": []} for t, a in decisions]
    cnt: dict[str, int] = {}
    for _, a in decisions:
        cnt[a] = cnt.get(a, 0) + 1
    nc.append_ledger({"row": "plan", "ts": _az(day, hh, mm).astimezone(timezone.utc).isoformat(timespec="seconds"),
                      "window_date": day.isoformat(), "mode": "apply", "decisions": rows, "counts": cnt}, ledger)


class TestHealthCheck:
    def test_no_row_before_the_fire_is_ok_after_it_is_a_warn(self, ledger):
        r = nhc.check_missed_nightly_catchup(now=_az(DAY_0910, 8, 0))
        assert r.status == "ok" and "has not happened" in r.detail
        r2 = nhc.check_missed_nightly_catchup(now=_az(DAY_0910, 8, 45))
        assert r2.status == "warn" and "NO catch-up decision row" in r2.detail and nc.TASK_NAME in r2.detail

    def test_all_fired_is_ok(self, ledger):
        _plan(ledger, DAY_0910, [("a", "fired"), ("b", "fired"), ("c", "skipped_disabled_in_set")])
        r = nhc.check_missed_nightly_catchup(now=_az(DAY_0910, 8, 45))
        assert r.status == "ok" and "every task fired" in r.detail and "fired=2" in r.detail

    def test_a_replay_or_cannot_check_is_a_warn_naming_the_task(self, ledger):
        _plan(ledger, DAY_0910, [("a", "fired"), ("cowork-cora-kb-sync-static", "run"), ("Cora - Drive Sweep", "cannot_check")])
        nc.append_ledger({"row": "task", "window_date": "2026-09-10", "task": "cowork-cora-kb-sync-static",
                          "action": "ran", "rc": 0, "duration_s": 12.0}, ledger)
        r = nhc.check_missed_nightly_catchup(now=_az(DAY_0910, 8, 45))
        assert r.status == "warn"
        assert "cowork-cora-kb-sync-static run" in r.detail and "Cora - Drive Sweep cannot_check" in r.detail

    def test_a_failed_replay_is_a_warn(self, ledger):
        _plan(ledger, DAY_0910, [("x", "run")])
        nc.append_ledger({"row": "task", "window_date": "2026-09-10", "task": "x", "action": "timeout", "rc": None}, ledger)
        r = nhc.check_missed_nightly_catchup(now=_az(DAY_0910, 9, 0))
        assert r.status == "warn" and "x replay timeout" in r.detail

    def test_registered_in_main_after_the_run_marker_check(self):
        import inspect
        src = inspect.getsource(nhc)
        body = src[src.index("def main("):]
        assert "all_results.append(check_missed_nightly_catchup())" in body
        assert body.index("all_results.append(check_run_markers())") < body.index("all_results.append(check_missed_nightly_catchup())")


class TestDigest:
    def test_section_totals_replays_and_missing_days(self, ledger, monkeypatch):
        today = datetime.now(nc.AZ).date()
        d1, d2 = today - timedelta(days=1), today - timedelta(days=2)
        _plan(ledger, d1, [("a", "fired"), ("b", "run")])
        nc.append_ledger({"row": "task", "window_date": d1.isoformat(), "task": "b", "action": "ran", "rc": 0}, ledger)
        _plan(ledger, d2, [("a", "fired"), ("b", "run")])
        nc.append_ledger({"row": "task", "window_date": d2.isoformat(), "task": "b", "action": "ran", "rc": 0}, ledger)
        s = chr_.missed_nightly_section(days=7)
        assert s["available"] and s["totals"] == {"fired": 2, "run": 2} and s["replayed_tasks"] == {"b": 2}
        assert s["missing_plan_days"] >= 4
        alarms = chr_.threshold_alarms({"missed_nightly": s})
        assert any("replayed on 2+" in a and "b x2" in a for a in alarms)
        assert any("catch-up lane itself is not firing" in a for a in alarms)
        line = chr_.format_slack({"missed_nightly": s, "alarms": alarms, "token_method": "t"})
        assert "*Missed-night catch-up (7d):*" in line and "replayed 2" in line and "b x2" in line

    def test_unavailable_section_is_fail_soft(self, monkeypatch):
        monkeypatch.setattr(nc, "read_ledger", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        s = chr_.missed_nightly_section()
        assert s["available"] is False and "boom" in s["reason"]
        assert chr_.threshold_alarms({"missed_nightly": s}) == []


# ── D-051 review group B regressions ─────────────────────────────────────────

class TestB1UnverifiedNightReadsWarn:
    def test_all_skipped_window_plan_row_at_1235_is_a_warn_naming_the_tasks(self, ledger, tmp_path):
        """B-1 FAILING INPUT: host down 02:00-12:30; both StartWhenAvailable tasks fire at
        boot ~12:35; the catch-up plans skipped_window x12 + skipped_disabled_in_set x4
        (nothing replayed, nothing verified). The old check said 'ok ... every task
        fired on schedule'. Nine ingest tasks were lost for the day."""
        window, tasks = nc.load_set(_REAL_SET)
        decisions = _decide(tasks, window, DAY_0910, _az(DAY_0910, 12, 35), tmp_path, [], _ready(tasks))
        assert nc.counts(decisions) == {"skipped_window": 16}      # ruled 9.7: no candidates left
        nc.append_ledger(nc.plan_row(DAY_0910, _az(DAY_0910, 12, 35), "apply", decisions), ledger)
        r = nhc.check_missed_nightly_catchup(now=_az(DAY_0910, 12, 35))
        assert r.status == "warn", r.detail
        assert "every task fired" not in r.detail
        assert "cowork-cora-kb-sync-gmail skipped_window (not verified)" in r.detail
        assert "Cora - Drive Sweep skipped_window (not verified)" in r.detail
        # 9.7 enabled the ex-candidates, so they are now unverified misses too (the
        # disabled-in-set-is-not-an-alarm property is pinned by test_ok_tail_is_derived_from_counts).
        assert "cowork-cora-qbo-token-refresh skipped_window (not verified)" in r.detail

    @pytest.mark.parametrize("action", ["skipped_not_due", "skipped_disabled", "cannot_check"])
    def test_an_enabled_task_with_no_evidence_is_never_ok(self, ledger, action):
        _plan(ledger, DAY_0910, [("a", "fired"), ("b", action)])
        r = nhc.check_missed_nightly_catchup(now=_az(DAY_0910, 8, 45))
        assert r.status == "warn" and f"b {action} (not verified)" in r.detail

    def test_ok_tail_is_derived_from_counts(self, ledger):
        _plan(ledger, DAY_0910, [("a", "fired"), ("b", "fired"), ("c", "skipped_already_ran"), ("d", "skipped_disabled_in_set")])
        nc.append_ledger({"row": "task", "window_date": "2026-09-10", "task": "c", "action": "ran", "rc": 0}, ledger)
        r = nhc.check_missed_nightly_catchup(now=_az(DAY_0910, 8, 45))
        assert r.status == "ok" and "1 replay(s) finished clean" in r.detail
        _plan(ledger, DAY_0909, [("a", "fired"), ("b", "fired"), ("d", "skipped_disabled_in_set")])
        r2 = nhc.check_missed_nightly_catchup(now=_az(DAY_0909, 9, 5))
        assert r2.status == "ok" and "every task fired on schedule (2/2 enabled)" in r2.detail

    def test_a_deferred_spawn_row_is_a_warn(self, ledger):
        _plan(ledger, DAY_0910, [("a", "run"), ("b", "run")])
        nc.append_ledger({"row": "task", "window_date": "2026-09-10", "task": "a", "action": "ran", "rc": 0}, ledger)
        nc.append_ledger({"row": "task", "window_date": "2026-09-10", "task": "b", "action": "not-started", "rc": None,
                          "reason": "budget"}, ledger)
        r = nhc.check_missed_nightly_catchup(now=_az(DAY_0910, 12, 40))
        assert r.status == "warn" and "b deferred at spawn time: not-started" in r.detail
        s = nc.summarize_day(nc.read_ledger(), DAY_0910)
        assert [x["task"] for x in s["replays"]] == ["a"] and [x["task"] for x in s["deferred"]] == ["b"]


class TestB2LiveSlug:
    def test_header_under_the_live_name_is_the_evidence_for_a_space_named_task(self, tmp_path):
        """B-2 FAILING INPUT: the live log is logs/tasks/Cora-Drive-Sweep-<date>.log; the old
        rule looked for Cora---Drive-Sweep-<date>.log (never written) -> `run` on a clean day."""
        _write_header(tmp_path, "Cora-Drive-Sweep", _az(DAY_0910, 6, 0, 2))
        assert nc.header_stamps(tmp_path, "Cora---Drive-Sweep", DAY_0910) == []
        window, tasks = nc.load_set(_REAL_SET)
        by = {d.task.name: d for d in _decide(tasks, window, DAY_0910, _az(DAY_0910, 9, 0), tmp_path, [], _ready(tasks))}
        assert by["Cora - Drive Sweep"].action == "fired"

    def test_no_scheduler_dry_run_reads_the_three_space_named_tasks_as_fired(self, ledger, tmp_path, capsys, monkeypatch):
        mod = _load_script()
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "task-runs.jsonl"))
        window, tasks = nc.load_set(_REAL_SET)
        for t in tasks:
            _write_header(tmp_path, LIVE_SLUGS.get(t.name, t.name), t.trigger_dt(DAY_0910) + timedelta(seconds=1))
        rc = mod.main(["--day", "2026-09-10", "--log-dir", str(tmp_path), "--no-scheduler", "--json"],
                      now_az=_az(DAY_0910, 9, 0))
        out = capsys.readouterr().out
        assert rc == 0
        plan = json.loads(out[out.index("{"):])
        by = {d["task"]: d["action"] for d in plan["decisions"]}
        assert {by[n] for n in ENABLED_TWELVE} == {"fired"}
        assert "run" not in by.values()

    def test_evidence_and_replay_use_the_live_action_name(self, ledger, tmp_path, monkeypatch):
        """A hand-registered action whose --name differs from the rule: the lane reads
        THAT file and replays under THAT slug (the header lands where the estate looks)."""
        mod = _load_script()
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "task-runs.jsonl"))
        set_yaml = tmp_path / "set.yaml"
        set_yaml.write_text("tasks:\n  - name: Cora - Drive Sweep\n    trigger_az: '06:00'\n    grace_min: 145\n    command: c\n"
                            "  - name: Cora - Drive Materialization\n    trigger_az: '05:45'\n    command: c2\n", encoding="utf-8")
        _write_header(tmp_path, "Odd-Live-Name", _az(DAY_0910, 6, 0, 2))
        wrapped = 'C:\\x\\deployment\\run_hidden.py --name Odd-Live-Name -- "C:\\x\\d.py" --with-slack'
        wrapped2 = 'C:\\x\\deployment\\run_hidden.py --name Cora-Drive-Materialization -- "C:\\x\\m.py"'
        states, actions = mod.parse_task_states(
            "Cora - Drive Sweep|Ready|2026-09-11T06:00:00||C:\\x\\pythonw.exe|" + wrapped + "\n"
            "Cora - Drive Materialization|Ready|2026-09-11T05:45:00||C:\\x\\pythonw.exe|" + wrapped2 + "\n")
        assert states["Cora - Drive Sweep"].slug == "Odd-Live-Name"
        calls = []

        class _Proc:
            def wait(self, timeout=None):
                return 0

        rc = mod.main(["--apply", "--set", str(set_yaml), "--log-dir", str(tmp_path)], now_az=_az(DAY_0910, 8, 30),
                      states_reader=lambda names: (states, actions), popen=lambda c, **k: calls.append(c) or _Proc())
        assert rc == 0
        assert len(calls) == 1 and "--name Cora-Drive-Materialization -- " in calls[0]   # Drive Sweep read as fired via Odd-Live-Name
        rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()]
        assert rows[0]["counts"] == {"fired": 1, "run": 1}
        assert [r for r in rows if r["row"] == "task"][0]["slug"] == "Cora-Drive-Materialization"

    def test_apply_with_no_scheduler_is_refused(self, ledger, tmp_path):
        mod = _load_script()
        spawned = []
        rc = mod.main(["--apply", "--no-scheduler", "--log-dir", str(tmp_path)], now_az=_az(DAY_0910, 8, 30),
                      popen=lambda *a, **k: spawned.append(a))
        assert rc == 2 and spawned == [] and not ledger.exists()


class TestB3PastDayDryRun:
    def test_past_day_dry_run_evaluates_inside_that_window(self, ledger, tmp_path, capsys, monkeypatch):
        """B-3 FAILING INPUT: `--day 2026-09-09 --no-scheduler` run at 19:37 AZ on 9/19
        printed skipped_window x12 -- zero `run` rows for the night that lost 13 tasks."""
        mod = _load_script()
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "task-runs.jsonl"))
        _shape_0909(tmp_path)
        spawned = []
        rc = mod.main(["--day", "2026-09-09", "--log-dir", str(tmp_path), "--no-scheduler"],
                      now_az=_az(date(2026, 9, 19), 19, 37), popen=lambda *a, **k: spawned.append(a))
        out = capsys.readouterr().out
        assert rc == 0 and spawned == [] and not ledger.exists()
        assert "evaluated as of 11:59:59 AZ on 2026-09-09" in out
        assert "run=16" in out.split("counts:")[1] and "skipped_window" not in out.split("counts:")[1]

    def test_now_override_pins_the_clock_for_a_dry_run(self, ledger, tmp_path, capsys, monkeypatch):
        mod = _load_script()
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "task-runs.jsonl"))
        rc = mod.main(["--day", "2026-09-09", "--now", "03:00", "--log-dir", str(tmp_path), "--no-scheduler"],
                      now_az=_az(date(2026, 9, 19), 19, 37))
        out = capsys.readouterr().out
        assert rc == 0 and "evaluated as of 03:00:00 AZ on 2026-09-09 (--now)" in out
        assert "skipped_not_due=16" in out.split("counts:")[1]       # nothing's deadline has passed at 03:00
        assert mod.main(["--day", "2026-09-09", "--now", "25:00", "--no-scheduler"], now_az=_az(date(2026, 9, 19), 19, 37)) == 2

    def test_apply_refusals_stay_on_the_real_clock(self, ledger, tmp_path):
        mod = _load_script()
        real = _az(date(2026, 9, 19), 19, 37)
        assert mod.main(["--apply", "--day", "2026-09-09", "--log-dir", str(tmp_path)], now_az=real,
                        states_reader=lambda n: ({}, {})) == 2
        assert mod.main(["--apply", "--now", "08:45", "--log-dir", str(tmp_path)], now_az=real,
                        states_reader=lambda n: ({}, {})) == 2
        assert not ledger.exists()


class TestB4DeadlinesBeforeTheFire:
    def test_every_shipped_deadline_is_strictly_before_the_lane_fire(self):
        _, tasks = nc.load_set(_REAL_SET)
        fire = nc.lane_fire_dt(DAY_0910)
        assert nc.LANE_FIRE_AZ == "08:30"
        for t in tasks:
            assert t.deadline_dt(DAY_0910) < fire, f"{t.name} deadline {t.deadline_dt(DAY_0910)} >= fire"
        by = {t.name: t for t in tasks}
        assert by["Cora - Drive Sweep"].grace_min == 145
        assert by["cowork-cora-info-for-cora-sweep"].grace_min == 130 and by["cowork-cora-gap-autofill"].grace_min == 130

    def test_flipped_candidates_and_drive_sweep_decide_run_at_the_fire(self, tmp_path):
        """B-4 FAILING INPUT: enabled: true on the 06:05/06:10 rows -> at the 08:30:05 fire
        they read skipped_not_due (deadline 08:35/08:40) and never replay; Drive Sweep at
        08:29:59 read skipped_not_due too (deadline exactly 08:30:00)."""
        _, tasks = nc.load_set(_REAL_SET)
        flipped = [nc.NightlyTask(name=t.name, trigger_az=t.trigger_az, grace_min=t.grace_min,
                                  max_minutes=t.max_minutes, enabled=True, command=t.command) for t in tasks
                   if t.trigger_az >= "06:00"]
        assert [t.name for t in flipped] == ["Cora - Drive Sweep", "cowork-cora-info-for-cora-sweep", "cowork-cora-gap-autofill"]
        at_fire = _decide(flipped, nc.Window(), DAY_0910, _az(DAY_0910, 8, 30, 5), tmp_path, [], _ready(flipped))
        assert [d.action for d in at_fire] == ["run", "run", "run"]
        early = _decide(flipped[:1], nc.Window(), DAY_0910, _az(DAY_0910, 8, 29, 59), tmp_path, [], _ready(flipped))
        assert early[0].action == "run"

    def test_load_set_refuses_a_deadline_at_or_after_the_fire(self, tmp_path):
        bad = tmp_path / "late.yaml"
        bad.write_text("tasks:\n  - name: ok\n    trigger_az: '04:00'\n  - name: late\n    trigger_az: '06:05'\n"
                       "    enabled: false\n  - name: exact\n    trigger_az: '06:00'\n", encoding="utf-8")
        with pytest.raises(ValueError) as ei:
            nc.load_set(bad)
        msg = str(ei.value)
        assert "late (06:05 + 150m = 08:35)" in msg and "exact (06:00 + 150m = 08:30)" in msg
        assert "ok (" not in msg
        mod = _load_script()
        assert mod.main(["--set", str(bad), "--no-scheduler"], now_az=_az(DAY_0910, 8, 45)) == 2

    def test_ps1_fire_time_and_limit_pin_the_module_constants(self):
        text = (_REPO / "deployment" / "setup-missed-nightly-catchup-task.ps1").read_text(encoding="ascii")
        assert f'$FireAt     = "{nc.LANE_FIRE_AZ}"' in text
        assert f"(New-TimeSpan -Hours {nc.LANE_BUDGET_MIN // 60})" in text and nc.LANE_BUDGET_MIN % 60 == 0
        assert "150-minute grace = 08:30" not in text          # the stale 'after every deadline' claim


class TestB5PerSpawnRecheck:
    def _set(self, tmp_path):
        set_yaml = tmp_path / "set.yaml"
        set_yaml.write_text(
            "tasks:\n"
            "  - name: cowork-cora-kb-sync-gmail\n    trigger_az: '02:30'\n    max_minutes: 180\n    command: gmail\n"
            "  - name: cowork-cora-kb-sync-asana\n    trigger_az: '03:00'\n    max_minutes: 60\n    command: asana\n"
            "  - name: cowork-cora-claude-mirror\n    trigger_az: '03:45'\n    max_minutes: 30\n    command: mirror\n"
            "  - name: cowork-cora-kb-sync-static\n    trigger_az: '04:00'\n    max_minutes: 60\n    command: static\n",
            encoding="utf-8")
        return set_yaml

    def test_recheck_rules(self):
        t = nc.NightlyTask(name="cowork-cora-claude-mirror", trigger_az="03:45")
        w = nc.Window()
        ok_state = nc.TaskState(state="Ready", next_run=_az(DAY_0910, 12, 15))
        assert nc.recheck_before_spawn(t, window=w, day=DAY_0910, now_az=_az(DAY_0910, 11, 0), state=ok_state, budget_left_s=9999) is None
        assert nc.recheck_before_spawn(t, window=w, day=DAY_0910, now_az=_az(DAY_0910, 12, 2), state=ok_state, budget_left_s=9999)[0] == "skipped_window"
        assert nc.recheck_before_spawn(t, window=w, day=DAY_0910, now_az=_az(DAY_0910, 11, 58), state=ok_state, budget_left_s=9999)[0] == "skipped_imminent"
        assert nc.recheck_before_spawn(t, window=w, day=DAY_0910, now_az=_az(DAY_0910, 11, 0), state=ok_state, budget_left_s=30)[0] == "not-started"
        assert nc.recheck_before_spawn(t, window=w, day=DAY_0910, now_az=_az(DAY_0910, 11, 0),
                                       state=nc.TaskState(state="Running"), budget_left_s=9999)[0] == "cannot_check"
        assert nc.recheck_before_spawn(t, window=w, day=DAY_0910, now_az=_az(DAY_0910, 11, 0), state=None, budget_left_s=9999) is None

    def test_long_gmail_replay_pushes_the_mirror_past_noon_and_it_is_not_spawned(self, ledger, tmp_path, monkeypatch):
        """B-5 FAILING INPUT: host down 02:15-05:30; gmail replays to 11:31, asana to 12:01;
        the mirror was spawned at ~12:02 and overlapped the scheduler's 12:15 mirror fire
        (two writers on the same files). Now: the re-check at 12:02 writes skipped_window."""
        mod = _load_script()
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "task-runs.jsonl"))
        set_yaml = self._set(tmp_path)
        now = {"t": _az(DAY_0910, 8, 30)}
        durations = {"gmail": 181, "asana": 30}      # minutes each replay "takes"
        calls = []

        class _Proc:
            def __init__(self, child):
                self.child = child

            def wait(self, timeout=None):
                now["t"] = now["t"] + timedelta(minutes=durations.get(self.child, 1))
                return 0

        def popen(cmdline, **kw):
            child = cmdline.split(" -- ", 1)[1]
            calls.append(child)
            return _Proc(child)

        def reader(names):
            return ({n: nc.TaskState(state="Ready", next_run=_az(DAY_0910, 12, 15) if "mirror" in n else _az(DAY_0911, 3, 0))
                     for n in names}, {})

        rc = mod.main(["--apply", "--set", str(set_yaml), "--log-dir", str(tmp_path)],
                      now_az=_az(DAY_0910, 8, 30), states_reader=reader, popen=popen, clock=lambda: now["t"])
        assert calls == ["gmail", "asana"], calls                      # mirror + static NOT spawned
        rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()]
        task_rows = {r["task"]: r for r in rows if r["row"] == "task"}
        assert task_rows["cowork-cora-kb-sync-gmail"]["action"] == "ran"
        assert task_rows["cowork-cora-kb-sync-asana"]["action"] == "ran"
        assert task_rows["cowork-cora-claude-mirror"]["action"] == "skipped_window"
        assert task_rows["cowork-cora-kb-sync-static"]["action"] == "skipped_window"
        assert rc == 1                                                # the night is NOT recovered
        # a deferred row never blocks a later honest replay of the same window
        _, tasks2 = nc.load_set(set_yaml)
        decisions = _decide(tasks2, nc.Window(), DAY_0910, _az(DAY_0910, 11, 0), tmp_path, [], _ready(tasks2), rows)
        by = {d.task.name: d.action for d in decisions}
        assert by["cowork-cora-kb-sync-gmail"] == "skipped_already_ran" and by["cowork-cora-claude-mirror"] == "run"

    def test_imminent_at_spawn_time_is_re_read_from_the_scheduler(self, ledger, tmp_path, monkeypatch):
        mod = _load_script()
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "task-runs.jsonl"))
        set_yaml = self._set(tmp_path)
        now = {"t": _az(DAY_0910, 8, 30)}
        calls = []

        class _Proc:
            def wait(self, timeout=None):
                now["t"] = now["t"] + timedelta(minutes=200)   # gmail runs to 11:50
                return 0

        def reader(names):
            # plan-time read: next runs far away; the per-spawn re-read (ONE name) says 12:00 (imminent at 11:50)
            nr = _az(DAY_0910, 12, 0) if len(names) == 1 else _az(DAY_0911, 3, 45)
            return ({n: nc.TaskState(state="Ready", next_run=nr) for n in names}, {})

        rc = mod.main(["--apply", "--set", str(set_yaml), "--log-dir", str(tmp_path)], now_az=_az(DAY_0910, 8, 30),
                      states_reader=reader, popen=lambda c, **k: calls.append(c.split(" -- ", 1)[1]) or _Proc(),
                      clock=lambda: now["t"])
        rows = {r["task"]: r for r in (json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()) if r["row"] == "task"}
        assert calls == ["gmail"]
        assert rows["cowork-cora-kb-sync-asana"]["action"] == "skipped_imminent"
        assert "imminent at spawn time" in rows["cowork-cora-kb-sync-asana"]["reason"]
        assert rc == 0     # imminent = the scheduler's own fire covers it

    def test_budget_exhaustion_writes_not_started_rows_and_caps_the_wait(self, ledger, tmp_path, monkeypatch):
        """The 960-minute enabled sum cannot fit the 4h limit: what does not fit is
        ledgered `not-started` instead of being killed mid-replay with no row."""
        mod = _load_script()
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "task-runs.jsonl"))
        set_yaml = self._set(tmp_path)
        now = {"t": _az(DAY_0910, 6, 30)}          # early enough that the window never closes first
        waits = []

        class _Proc:
            def wait(self, timeout=None):
                waits.append(timeout)
                now["t"] = now["t"] + timedelta(seconds=timeout)   # each replay uses its whole allowance
                return 0

        def reader(names):
            return ({n: nc.TaskState(state="Ready", next_run=_az(DAY_0911, 3, 0)) for n in names}, {})

        rc = mod.main(["--apply", "--set", str(set_yaml), "--log-dir", str(tmp_path)], now_az=_az(DAY_0910, 6, 30),
                      states_reader=reader, popen=lambda c, **k: _Proc(), clock=lambda: now["t"])
        rows = {r["task"]: r for r in (json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()) if r["row"] == "task"}
        assert waits == [180 * 60, 60 * 60]                       # gmail 180m + asana 60m = the 240m budget
        assert rows["cowork-cora-claude-mirror"]["action"] == "not-started"
        assert rows["cowork-cora-kb-sync-static"]["action"] == "not-started"
        assert "budget" in rows["cowork-cora-claude-mirror"]["reason"] and rc == 1
        # the wait is capped by the budget LEFT, not just max_minutes
        now["t"] = _az(DAY_0910, 6, 30)
        waits.clear()
        ledger.unlink()
        set2 = tmp_path / "set2.yaml"
        set2.write_text("tasks:\n  - name: a\n    trigger_az: '02:00'\n    max_minutes: 200\n    command: a\n"
                        "  - name: b\n    trigger_az: '03:00'\n    max_minutes: 200\n    command: b\n", encoding="utf-8")
        mod.main(["--apply", "--set", str(set2), "--log-dir", str(tmp_path)], now_az=_az(DAY_0910, 6, 30),
                 states_reader=reader, popen=lambda c, **k: _Proc(), clock=lambda: now["t"])
        assert waits == [200 * 60, 40 * 60]

    def test_lane_budget_matches_the_registered_limit(self):
        assert nc.LANE_BUDGET_MIN == 240
        _, tasks = nc.load_set(_REAL_SET)
        assert sum(t.max_minutes for t in tasks if t.enabled) > nc.LANE_BUDGET_MIN   # the premise the budget guards


# ── Code #14 R14-7 (4 + 4a): T1 records its plan (ruling 9.2) ────────────────
#
# Under T1 (no --apply) the lane returned before writing its plan row or its run
# marker, so the 08:45 check WARNed daily "NO catch-up decision row" + "no run
# marker ever recorded" while the task DID fire (logs/tasks/...-2026-09-22.log:
# "dry-run ... counts: fired=16 ... EXIT: 0"). --record writes the plan (mode
# dry-run) + a plan-only marker; a plain dry run still writes nothing.

_T1_SET = ("tasks:\n"
           "  - name: cowork-cora-kb-sync-slack\n    trigger_az: '02:00'\n    command: slack\n"
           "  - name: cowork-cora-kb-sync-static\n    trigger_az: '04:00'\n    command: static\n")


def _t1_reader(names):
    return ({n: nc.TaskState(state="Ready", next_run=_az(DAY_0910, 12, 20)) for n in names}, {})


class TestR147RecordT1Plan:
    def test_record_writes_one_dry_run_plan_row_and_one_plan_only_marker(self, ledger, tmp_path, monkeypatch):
        mod = _load_script()
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "task-runs.jsonl"))
        set_yaml = tmp_path / "set.yaml"
        set_yaml.write_text(_T1_SET, encoding="utf-8")
        _write_header(tmp_path, "cowork-cora-kb-sync-slack", _az(DAY_0910, 2, 0, 1))   # static missed
        spawned = []
        rc = mod.main(["--record", "--set", str(set_yaml), "--log-dir", str(tmp_path)],
                      now_az=_az(DAY_0910, 8, 30), states_reader=_t1_reader,
                      popen=lambda *a, **k: spawned.append(a))
        assert rc == 0 and spawned == []                              # T1 replays NOTHING
        rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1 and rows[0]["row"] == "plan" and rows[0]["mode"] == "dry-run"
        assert rows[0]["window_date"] == "2026-09-10" and rows[0]["counts"] == {"fired": 1, "run": 1}
        markers = run_marker.read_markers()
        assert len(markers) == 1
        m = markers[0]
        assert m["task"] == nc.TASK_NAME and m["ok"] is True and m["outputs"] == 0
        assert m["outcome"] == "plan-only" and m["catch_up"] is True and m["window"] == "2026-09-10"
        assert m["would_replay"] == ["cowork-cora-kb-sync-static"]

    @pytest.mark.parametrize("extra", [
        ["--day", "2026-09-09"],
        ["--now", "08:45"],
        ["--no-scheduler"],
        ["--apply"],
    ])
    def test_record_refuses_a_pinned_or_blind_run_and_apply(self, ledger, tmp_path, monkeypatch, extra):
        mod = _load_script()
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "task-runs.jsonl"))
        spawned = []
        rc = mod.main(["--record", *extra, "--log-dir", str(tmp_path)], now_az=_az(DAY_0910, 8, 30),
                      states_reader=_t1_reader, popen=lambda *a, **k: spawned.append(a))
        assert rc == 2 and spawned == []
        assert not ledger.exists() and run_marker.read_markers() == []

    def test_plain_dry_run_still_writes_nothing(self, ledger, tmp_path, monkeypatch):
        """The pinned 'dry-run writes nothing' contract is untouched by --record."""
        mod = _load_script()
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "task-runs.jsonl"))
        set_yaml = tmp_path / "set.yaml"
        set_yaml.write_text(_T1_SET, encoding="utf-8")
        rc = mod.main(["--set", str(set_yaml), "--log-dir", str(tmp_path)], now_az=_az(DAY_0910, 8, 30),
                      states_reader=_t1_reader)
        assert rc == 0 and not ledger.exists() and run_marker.read_markers() == []


def _t1_plan(ledger: Path, day: date, decisions: list[tuple[str, str]]) -> None:
    rows = [{"task": t, "trigger_az": "04:00", "action": a, "reason": "r", "evidence": []} for t, a in decisions]
    cnt: dict[str, int] = {}
    for _, a in decisions:
        cnt[a] = cnt.get(a, 0) + 1
    nc.append_ledger({"row": "plan", "ts": _az(day, 8, 30).astimezone(timezone.utc).isoformat(timespec="seconds"),
                      "window_date": day.isoformat(), "mode": "dry-run", "decisions": rows, "counts": cnt}, ledger)


class TestR147HealthReadsT1:
    def test_a_t1_plan_where_everything_fired_is_ok_fired_t1_plan_recorded(self, ledger):
        _t1_plan(ledger, DAY_0910, [("a", "fired"), ("b", "fired"), ("c", "skipped_disabled_in_set")])
        r = nhc.check_missed_nightly_catchup(now=_az(DAY_0910, 8, 45))
        assert r.status == "ok" and "fired (T1 plan recorded)" in r.detail and "(2/2 enabled)" in r.detail

    def test_a_t1_plan_with_run_rows_warns_would_have_replayed_not_replayed(self, ledger):
        _t1_plan(ledger, DAY_0910, [("a", "fired"), ("cowork-cora-kb-sync-static", "run"),
                                    ("Cora - Drive Sweep", "run")])
        r = nhc.check_missed_nightly_catchup(now=_az(DAY_0910, 8, 45))
        assert r.status == "warn"
        assert ("T1: would have replayed 2 task(s) (not replayed): cowork-cora-kb-sync-static, "
                "Cora - Drive Sweep") in r.detail
        assert "cowork-cora-kb-sync-static run" not in r.detail   # not the T2 "pending replay" form

    def test_a_t1_plan_keeps_the_unverified_and_skipped_window_warns(self, ledger):
        _t1_plan(ledger, DAY_0910, [("a", "fired"), ("b", "skipped_window"), ("c", "cannot_check")])
        r = nhc.check_missed_nightly_catchup(now=_az(DAY_0910, 12, 35))
        assert r.status == "warn"
        assert "b skipped_window (not verified)" in r.detail and "c cannot_check (not verified)" in r.detail
        assert "T1: would have replayed" not in r.detail

    def test_the_t2_apply_plan_reading_is_unchanged(self, ledger):
        _plan(ledger, DAY_0910, [("a", "fired"), ("x", "run")])
        r = nhc.check_missed_nightly_catchup(now=_az(DAY_0910, 8, 45))
        assert r.status == "warn" and "x run" in r.detail and "T1:" not in r.detail


class TestR147SetupPs1:
    _PS1 = _REPO / "deployment" / "setup-missed-nightly-catchup-task.ps1"

    def _code_lines(self) -> list[str]:
        text = self._PS1.read_text(encoding="ascii")
        return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]

    def test_param_switch_is_declared_before_the_error_preference(self):
        code = "\n".join(self._code_lines())
        assert "param([switch]$Apply)" in code
        assert code.index("param([switch]$Apply)") < code.index('$ErrorActionPreference = "Stop"')

    def test_default_argument_records_and_apply_appears_only_inside_the_if(self):
        lines = self._code_lines()
        default = [ln for ln in lines if ln.strip().startswith("$ArgLine =")]
        assert default and "--record" in default[0] and "--apply" not in default[0]
        code = "\n".join(lines)
        if_start = code.index("if ($Apply) {")
        if_end = code.index("}", if_start)
        apply_hits = [i for i in range(len(code)) if code.startswith("--apply", i)]
        assert apply_hits and all(if_start < i < if_end for i in apply_hits)
        assert "-Argument $ArgLine" in code

    def test_header_and_description_say_t1_by_default(self):
        text = self._PS1.read_text(encoding="ascii")
        assert "Runs scripts/check_missed_nightly.py --apply once a day" not in text
        assert ("T1 (dry-run plan, recorded) by default per the 2026-09-19 ruling 9.2; "
                "-Apply registers T2 act-with-audit") in text
        assert "T2 act-with-audit pending Harrison's tier confirm" not in text
        assert 'Write-Host "  Mode: $Mode"' in text
