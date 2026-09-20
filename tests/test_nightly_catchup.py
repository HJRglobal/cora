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
ENABLED_TWELVE = [
    "cowork-cora-kb-sync-slack", "cowork-cora-kb-sync-gmail", "cowork-cora-kb-sync-asana",
    "cowork-cora-kb-sync-fireflies", "cowork-cora-claude-mirror", "cowork-cora-kb-sync-static",
    "cowork-cora-kb-sync-drive", "Cora - LEX Dump Folder Sync", "cowork-cora-kb-sync-notion",
    "cowork-cora-session-capture", "Cora - Drive Materialization", "Cora - Drive Sweep",
]
CANDIDATES_FOUR = ["cowork-cora-qbo-token-refresh", "cowork-cora-reconciliation",
                   "cowork-cora-info-for-cora-sweep", "cowork-cora-gap-autofill"]


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

class TestSlugAndSet:
    @pytest.mark.parametrize("name", ENABLED_TWELVE + CANDIDATES_FOUR + [
        "Cora - Daily Synthesis (F3E)", "", "..weird..", "a" * 100, "Cora - Missed Nightly Catch-Up"])
    def test_slug_rule_matches_the_launcher_byte_for_byte(self, name):
        rh = _load_launcher()
        assert nc.sanitize_slug(name) == rh.sanitize_slug(name)

    def test_shipped_set_loads_sorted_with_twelve_enabled_and_four_candidates(self):
        window, tasks = nc.load_set(_REAL_SET)
        assert window.start_az == "06:00" and window.end_az == "12:00"
        names = [t.name for t in tasks]
        assert [t.name for t in tasks if t.enabled] == ENABLED_TWELVE
        assert sorted(t.name for t in tasks if not t.enabled) == sorted(CANDIDATES_FOUR)
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
        assert runs == ENABLED_TWELVE
        assert {d.task.name for d in decisions if d.action == "skipped_disabled_in_set"} == set(CANDIDATES_FOUR)
        assert not [d for d in decisions if d.action == "fired"]

    def test_0910_selects_nothing(self, tmp_path):
        window, tasks = nc.load_set(_REAL_SET)
        _shape_0910(tmp_path, tasks)
        decisions = _decide(tasks, window, DAY_0910, _az(DAY_0910, 8, 45), tmp_path, [], _ready(tasks))
        assert [d.task.name for d in decisions if d.action == "run"] == []
        assert sorted(d.task.name for d in decisions if d.action == "fired") == sorted(ENABLED_TWELVE)

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
