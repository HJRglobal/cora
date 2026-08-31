"""Run-marker contract (session #11 S4) -- retires the cq-a251dee3f5cf class.

"A task that fires and writes nothing is indistinguishable from one that never
fired." The weekly Slack-clarity check fired 8/22 with a registry-confirmed
lastRunAt and posted no digest; nothing recorded it. Task Scheduler sees exit
codes, not outputs.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.cora import run_marker

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    p = tmp_path / "task-runs.jsonl"
    monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(p))
    return p


class TestWrite:
    def test_appends_one_json_line_per_run(self, ledger):
        assert run_marker.write("job-a", outputs=3) is True
        assert run_marker.write("job-a", outputs=0, ok=False) is True
        lines = [l for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["task"] == "job-a" and first["outputs"] == 3 and first["ok"] is True

    def test_outputs_is_an_int_count_not_a_flag(self):
        """The whole contract: a run that produced 0 things is the alarm, and
        that cannot be expressed by a boolean."""
        assert run_marker.write.__doc__ and "count" in run_marker.write.__doc__

    def test_never_raises_and_reports_failure(self, tmp_path, monkeypatch, caplog):
        """Observability must not be able to take down the lane it watches --
        but it must also not fail SILENTLY (D-133), or "wrote nothing" becomes
        indistinguishable from "never ran", which is the very ambiguity being
        closed."""
        # point the ledger at a path that cannot be created
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "afile" / "x.jsonl"))
        (tmp_path / "afile").write_text("not a directory", encoding="utf-8")
        with caplog.at_level("ERROR"):
            ok = run_marker.write("job-a", outputs=1)
        assert ok is False
        assert run_marker.WRITE_FAIL_TOKEN in caplog.text

    def test_failure_token_is_a_health_check_critical(self):
        """The token must be one the nightly check actually alarms on -- a
        sentinel nothing matches is decoration."""
        import io

        src = io.open("scripts/nightly_health_check.py", encoding="utf-8").read()
        assert run_marker.WRITE_FAIL_TOKEN in src

    def test_no_read_modify_write(self, ledger):
        """Append-only is what makes it safe across 93 separate processes."""
        run_marker.write("a", outputs=1)
        ledger.write_text(ledger.read_text(encoding="utf-8") + "GARBAGE\n", encoding="utf-8")
        run_marker.write("b", outputs=1)
        # the malformed line survives untouched; the writer never rewrites the file
        assert "GARBAGE" in ledger.read_text(encoding="utf-8")
        assert len(run_marker.read_markers()) == 2   # and the reader skips it


class TestLatestByTask:
    def test_picks_the_newest_row_per_task(self, ledger):
        rows = [
            {"ts": "2026-09-01T00:00:00+00:00", "task": "a", "outputs": 1},
            {"ts": "2026-09-09T00:00:00+00:00", "task": "a", "outputs": 7},
            {"ts": "2026-09-05T00:00:00+00:00", "task": "b", "outputs": 2},
        ]
        ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        latest = run_marker.latest_by_task()
        assert latest["a"]["outputs"] == 7
        assert latest["b"]["outputs"] == 2

    def test_missing_ledger_is_empty_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TASK_RUNS_LEDGER_PATH", str(tmp_path / "nope.jsonl"))
        assert run_marker.latest_by_task() == {}


REG = [{"name": "weekly-job", "cadence_hours": 168, "expects_output": True,
        "registered": "2026-08-01T00:00:00+00:00"}]


def _marker(hours_ago: float, outputs: int = 5, ok: bool = True, **extra) -> dict:
    row = {"ts": (NOW - timedelta(hours=hours_ago)).isoformat(),
           "outputs": outputs, "ok": ok}
    row.update(extra)
    return {"weekly-job": row}


class TestTheTwoAlarmsBothFire:
    """A rail that cannot fail on the regression it guards is not a rail."""

    def test_fired_but_wrote_nothing(self):
        """THE cq-a251dee3f5cf SHAPE: recent marker, exit fine, zero outputs.
        Invisible to Task Scheduler, which only ever sees exit code 0."""
        findings = run_marker.evaluate(REG, _marker(3, outputs=0), now=NOW)
        assert findings and "FIRED BUT WROTE NOTHING" in findings[0][1]

    def test_missed_fire(self):
        findings = run_marker.evaluate(REG, _marker(400), now=NOW)
        assert findings and "MISSED FIRE" in findings[0][1]

    def test_the_two_alarms_are_distinct(self):
        """Conflating them would hide the second, which is the one nothing else
        in the estate can see."""
        wrote_nothing = run_marker.evaluate(REG, _marker(3, outputs=0), now=NOW)[0][1]
        missed = run_marker.evaluate(REG, _marker(400), now=NOW)[0][1]
        assert wrote_nothing != missed

    def test_error_outcome_surfaces(self):
        findings = run_marker.evaluate(REG, _marker(3, ok=False, detail="boom"), now=NOW)
        assert findings and "reports an error" in findings[0][1]

    def test_unparseable_timestamp_is_surfaced_not_swallowed(self):
        findings = run_marker.evaluate(REG, {"weekly-job": {"ts": "nope"}}, now=NOW)
        assert findings and "unparseable" in findings[0][1]


class TestQuietCasesStayQuiet:
    def test_healthy_run_produces_no_finding(self):
        assert run_marker.evaluate(REG, _marker(3, outputs=5), now=NOW) == []

    def test_one_skipped_fire_is_not_an_alarm(self):
        """The window is 2x cadence so a single missed weekly fire is not a
        nightly false alarm."""
        assert run_marker.evaluate(REG, _marker(170), now=NOW) == []

    def test_expects_output_false_tolerates_zero(self):
        reg = [{"name": "quiet", "cadence_hours": 24, "expects_output": False,
                "registered": "2026-08-01T00:00:00+00:00"}]
        markers = {"quiet": {"ts": (NOW - timedelta(hours=1)).isoformat(), "outputs": 0}}
        assert run_marker.evaluate(reg, markers, now=NOW) == []


class TestMissingMarkerIsNotExcused:
    """The existing info-for-cora reader returns OK when its marker file is
    absent, which makes a never-adopted lane look healthy forever. The grace here
    is gated on the REGISTRATION date, not on file absence."""

    def test_long_registered_with_no_marker_alarms(self):
        findings = run_marker.evaluate(REG, {}, now=NOW)
        assert findings and "no run marker ever recorded" in findings[0][1]

    def test_freshly_registered_is_given_its_first_window(self):
        reg = [{"name": "new-job", "cadence_hours": 168, "expects_output": True,
                "registered": (NOW - timedelta(hours=10)).isoformat()}]
        assert run_marker.evaluate(reg, {}, now=NOW) == []


class TestRegistryAndWiring:
    def test_registry_entries_are_wellformed(self):
        import yaml

        data = yaml.safe_load(
            Path("data/maps/scheduled-task-state.yaml").read_text(encoding="utf-8"))
        entries = data.get("run_markers") or []
        assert entries, "run_markers registry is empty"
        for e in entries:
            assert e.get("name")
            assert float(e.get("cadence_hours") or 0) > 0
            assert "expects_output" in e
            assert e.get("registered")

    def test_registered_tasks_actually_write_markers(self):
        """A registry entry whose task never calls the helper would alarm
        forever. Every expects_output task listed must be instrumented."""
        import io

        import yaml

        data = yaml.safe_load(
            Path("data/maps/scheduled-task-state.yaml").read_text(encoding="utf-8"))
        scripts = {
            "cowork-cora-meeting-capture-audit": "scripts/run_meeting_capture_audit.py",
            "cowork-cora-finance-close-pack": "scripts/run_finance_close_pack.py",
            "Cora - F3E Blog Pipeline": "scripts/run_f3e_blog_pipeline.py",
        }
        for entry in data.get("run_markers") or []:
            path = scripts.get(entry["name"])
            assert path, "registry names %r with no known script" % entry["name"]
            src = io.open(path, encoding="utf-8").read()
            assert "run_marker.write(" in src, "%s never writes a marker" % path

    def test_health_check_exposes_the_check(self):
        import io

        src = io.open("scripts/nightly_health_check.py", encoding="utf-8").read()
        assert "def check_run_markers(" in src
        assert "all_results.append(check_run_markers())" in src
