"""Task-estate manifest generator + drift monitor (DR/VM step 1, slice M1; cq-a296aa8e0a2e).

Contract under test:
  * diff_manifests is FAIL-CAPABLE (charter section 1): a fixture with one injected fake
    task + one removed real task yields EXACTLY those two WARN lines and nothing else;
    cron / enabled / action / principal changes each raise their own named line;
    volatile fields (last run, next run, last result, log mtimes, marker times) never do;
  * trigger_text renders daily / weekly (bitmask AND XML day names) / monthly
    (ScheduleByMonth, ScheduleByMonthDayOfWeek) / minute-repetition / logon triggers as
    stable, restorable text -- never 'trigger from <date>' when calendar detail exists;
  * normalize_task strips the run_hidden wrapper to the child command, finds the script,
    derives the log slug the estate really writes, and reads intent from
    scheduled-task-state.yaml; a disabled task not in the disabled list is intent DRIFT;
  * the GENERATED doc blocks are idempotent (a second render of the same manifest is
    byte-identical) and the marker replacement refuses a doc without exactly one pair;
  * the COMMITTED manifest (deployment/manifest/task-estate.json) has the schema, counts
    its own tasks, records the three-method enumeration cross-check, and carries no
    secret shape (actions are paths + flags);
  * cora_health_report.task_estate_section is fail-soft and its alarm fires only on drift;
  * on Windows, the live enumeration returns >= 1 task and the three methods are
    recorded (agreement or the named disagreement), so the generator never runs blind.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "src"))

import generate_task_estate_manifest as tem  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "task_estate_small.json"
COMMITTED = REPO / "deployment" / "manifest" / "task-estate.json"

_SECRET_SHAPES = (
    re.compile(r"xox[abpers]-[A-Za-z0-9-]{10,}"), re.compile(r"xapp-\d-[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"), re.compile(r"sk-(?:ant-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"hc-ping\.com/[0-9a-f-]{36}"), re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"\d/\d{16}:[A-Za-z0-9]{32}"), re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _raw() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _norm(raw: list[dict], tmp_path: Path) -> list[dict]:
    intent = {"available": True, "running": {"cowork-cora-service"}, "disabled": set(), "enabled": set(),
              "run_markers": {"cowork-cora-backup": {"name": "cowork-cora-backup"}}}
    scripts = {"setup-windows-task.ps1": '$TASK_NAME    = "cowork-cora-service"',
               "setup-backup-task.ps1": '$TaskName = "cowork-cora-backup"',
               "setup-kb-evals-task.ps1": '# Schedule: weekly Monday 09:05 AZ -- BEFORE "Cora - Weekly Health Metrics"\n$TaskName = "Cora - KB Evals"',
               "setup-weekly-health-metrics-task.ps1": "$TaskName = 'Cora - Weekly Health Metrics'",
               "remove-backup-task.ps1": 'Unregister-ScheduledTask -TaskName "cowork-cora-backup"'}
    ladder = [("claude-workspace-mirror", "T0", "claude-workspace mirror (scripts/mirror_claude_workspace.py)")]
    markers = {"cowork-cora-backup": {"ts": "2026-09-22T03:30:00+00:00", "ok": True}}
    return [tem.normalize_task(r, intent=intent, scripts=scripts, ladder=ladder, markers=markers, log_dir=tmp_path)
            for r in raw]


# ── trigger rendering ─────────────────────────────────────────────────────────
class TestTriggerText:
    def test_daily_weekly_logon(self):
        assert tem.trigger_text({"type": "MSFT_TaskDailyTrigger", "start_boundary": "2026-06-09T20:30:00", "days_interval": 1}) == "daily 20:30"
        assert tem.trigger_text({"type": "MSFT_TaskWeeklyTrigger", "start_boundary": "2026-06-08T09:30:00-07:00",
                                 "days_of_week": 2, "weeks_interval": 1}) == "weekly Mon 09:30"
        assert tem.trigger_text({"type": "MSFT_TaskWeeklyTrigger", "start_boundary": "2026-06-08T07:00:00",
                                 "days_of_week": 62, "weeks_interval": 1}) == "weekly Mon,Tue,Wed,Thu,Fri 07:00"
        assert tem.trigger_text({"type": "MSFT_TaskLogonTrigger"}) == "at logon"

    def test_minute_repetition_reads_as_every(self):
        t = tem.trigger_text({"type": "MSFT_TaskTimeTrigger", "start_boundary": "2026-07-16T10:27:00", "rep_interval": "PT5M"})
        assert t == "every PT5M (from 2026-07-16T10:27)"

    def test_repetition_duration_is_part_of_the_restorable_text(self):
        """A repeating daily trigger's DURATION (and stop-at-end) is restore-relevant and
        diff-relevant: PT13H vs P1D is a different task (D-051 lens A)."""
        base = {"type": "MSFT_TaskDailyTrigger", "start_boundary": "2026-08-25T07:08:00", "days_interval": 1, "rep_interval": "PT15M"}
        assert tem.trigger_text(dict(base, rep_duration="PT13H", rep_stop_at_end=True)) == "every PT15M for PT13H stop-at-end (daily 07:08)"
        assert tem.trigger_text(dict(base, rep_duration="P1D", rep_stop_at_end=False)) == "every PT15M for P1D (daily 07:08)"
        assert tem.trigger_text(dict(base, rep_duration="PT13H")) != tem.trigger_text(dict(base, rep_duration="P1D"))

    def test_monthly_from_xml_detail_is_restorable(self):
        t = tem.trigger_text({"type": "XML:CalendarTrigger/ScheduleByMonth", "start_boundary": "2026-06-09T14:00:00",
                              "days_of_month": ["1"], "months": ["January", "February", "March", "April", "May", "June",
                                                                  "July", "August", "September", "October", "November", "December"]})
        assert t == "monthly day 1 14:00"
        t2 = tem.trigger_text({"type": "XML:CalendarTrigger/ScheduleByMonthDayOfWeek", "start_boundary": "2026-08-25T09:38:00",
                               "weeks_of_month": ["1"], "days_of_week_names": ["Tuesday"], "months": ["March", "June"]})
        assert t2 == "monthly week 1 Tue 09:38 in Mar,Jun"

    def test_disabled_trigger_is_marked(self):
        assert tem.trigger_text({"type": "MSFT_TaskDailyTrigger", "start_boundary": "2026-06-09T20:30:00", "enabled": False}).endswith("[trigger disabled]")

    def test_xml_parser_reads_schtasks_monthly(self):
        xml = ('<?xml version="1.0" encoding="UTF-16"?>\n<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
               '<Triggers><CalendarTrigger><StartBoundary>2026-06-09T14:00:00</StartBoundary><Enabled>true</Enabled>'
               '<ScheduleByMonth><DaysOfMonth><Day>1</Day></DaysOfMonth><Months><January /><July /></Months></ScheduleByMonth>'
               '</CalendarTrigger><TimeTrigger><StartBoundary>2026-07-16T10:27:00</StartBoundary>'
               '<Repetition><Interval>PT5M</Interval><Duration>P1D</Duration></Repetition></TimeTrigger></Triggers></Task>')
        rows = tem.triggers_from_xml(xml)
        assert [r["type"] for r in rows] == ["XML:CalendarTrigger/ScheduleByMonth", "XML:TimeTrigger"]
        assert rows[0]["days_of_month"] == ["1"] and rows[0]["months"] == ["January", "July"]
        assert tem.trigger_text(rows[0]) == "monthly day 1 14:00 in Jan,Jul"
        assert tem.trigger_text(rows[1]) == "every PT5M for P1D (from 2026-07-16T10:27)"   # the Duration is part of the schedule (review MEDIUM)

    def test_xml_parser_never_raises_on_garbage(self):
        assert tem.triggers_from_xml("<not xml") == []


# ── normalisation ─────────────────────────────────────────────────────────────
class TestNormalize:
    def test_strips_run_hidden_and_finds_script_slug_intent(self, tmp_path):
        tasks = {t["name"]: t for t in _norm(_raw(), tmp_path)}
        svc = tasks["cowork-cora-service"]
        assert svc["child_command"].endswith("python.exe -m cora.main") and svc["script"] == "cora.main"
        assert svc["windowless"] is True and svc["intent"] == "running" and svc["trigger_text"] == "at logon"
        bk = tasks["cowork-cora-backup"]
        assert bk["script"] == "scripts/backup_logs.py" and bk["log_slug"] == "cowork-cora-backup"
        assert bk["run_marker"] == {"registered": True, "present": True, "last_ts": "2026-09-22T03:30:00+00:00"}
        assert bk["setup_scripts"] == ["setup-backup-task.ps1"]          # remove-*.ps1 never counts
        whm = tasks["Cora - Weekly Health Metrics"]
        assert whm["log_slug"] == "Cora-Weekly-Health-Metrics" and whm["trigger_text"] == "weekly Mon 09:30"
        assert whm["setup_scripts"] == ["setup-weekly-health-metrics-task.ps1"]   # the comment mention does NOT count
        assert whm["run_as"] == {"user_id": "S-1-5-21-0-0-0-1001", "logon_type": "Interactive", "run_level": "Highest"}
        lc = tasks["Cora - Log Compaction"]
        assert lc["enabled"] is False and lc["windowless"] is False
        assert lc["script"] == "scripts/compact_logs.py" and lc["trigger_text"] == "monthly day 1 14:00"
        assert lc["intent"] == "UNROWED" and lc["intent_drift"] == "host DISABLED, not in the disabled list"
        assert lc["setup_scripts"] == [] and lc["start_when_available"] is False

    def test_intent_drift_both_directions(self):
        intent = {"running": set(), "disabled": {"x"}, "enabled": {"y"}, "run_markers": {}}
        assert tem.intent_for("x", True, intent) == ("disabled", "host ENABLED but intent disabled")
        assert tem.intent_for("x", False, intent) == ("disabled", "")
        assert tem.intent_for("y", False, intent) == ("enabled", "host DISABLED but intent enabled")
        assert tem.intent_for("z", True, intent) == ("UNROWED", "")

    def test_ladder_lane_heuristic_by_script_name(self):
        idx = [("claude-workspace-mirror", "T0", "claude-workspace mirror (scripts/mirror_claude_workspace.py)")]
        assert tem.ladder_lane_for("cowork-cora-claude-mirror", "scripts/mirror_claude_workspace.py", idx) == "claude-workspace-mirror (T0)"
        assert tem.ladder_lane_for("cowork-cora-backup", "scripts/backup_logs.py", idx) == "UNROWED"
        assert tem.ladder_lane_for("cowork-cora-service", "cora.main", idx) == "UNROWED"

    def test_running_intent_reports_a_disabled_service_as_drift(self):
        intent = {"running": {"cowork-cora-service"}, "disabled": set(), "enabled": set(), "run_markers": {}}
        assert tem.intent_for("cowork-cora-service", False, intent) == ("running", "host DISABLED but intent running")
        assert tem.intent_for("cowork-cora-service", True, intent) == ("running", "")

    def test_registration_match_ignores_comments_and_non_create_tn_lines(self):
        """The 2026-09-23 review finding: a `# schtasks /Change /TN "<other task>" /Disable` comment
        attributed the wrong setup script. Only non-comment registration shapes count."""
        scripts = {
            "setup-daily-synthesis-f3e-task.ps1": '#     schtasks /Change /TN "Cora - F3E Daily Ecom Brief" /Disable\n$TaskName = "Cora - Daily Synthesis (F3E)"\n',
            "setup-f3e-ecom-brief-task.ps1": '$TaskName = "Cora - F3E Daily Ecom Brief"\n',
            "setup-compaction-task.ps1": 'schtasks /Create /TN "Cora - Log Compaction" /SC MONTHLY /D 1 /ST 14:00\n# schtasks /Delete /TN "Cora - Other" /F\n',
            "setup-other.ps1": '<# multi-line block\n$TaskName = "Cora - Log Compaction"\n#>\nWrite-Host "nothing"\n',
        }
        assert tem.setup_scripts_for("Cora - F3E Daily Ecom Brief", scripts) == ["setup-f3e-ecom-brief-task.ps1"]
        assert tem.setup_scripts_for("Cora - Log Compaction", scripts) == ["setup-compaction-task.ps1"]
        assert tem.setup_scripts_for("Cora - Other", scripts) == []

    def test_normalized_task_carries_its_xml_for_the_restore_set(self, tmp_path):
        raw = _raw()
        raw[0]["_xml"] = '<?xml version="1.0" encoding="UTF-16"?><Task/>'
        tasks = _norm(raw, tmp_path)
        assert tasks[0]["_xml"].startswith("<?xml")
        m = tem.build_manifest(copy.deepcopy(tasks), host="fixture")
        assert "_xml" not in m["tasks"][0] and m["_task_xml"][tasks[0]["log_slug"]].startswith("<?xml")
        out = tmp_path / "manifest"
        written = tem.write_outputs(m, out)
        xml_files = sorted((out / tem.TASK_XML_SUBDIR).glob("*.xml"))
        assert [p.name for p in xml_files] == [f"{tasks[0]['log_slug']}.xml"]      # only the task that had XML
        assert xml_files[0].read_text(encoding="utf-8").startswith('<?xml version="1.0" encoding="UTF-8"?>')
        assert "_task_xml" not in json.loads((out / tem.MANIFEST_JSON).read_text(encoding="utf-8"))
        # a stale XML for a task no longer in the manifest is removed on the next write
        stale = out / tem.TASK_XML_SUBDIR / "Cora-Gone.xml"; stale.write_text("<Task/>", encoding="utf-8")
        tem.write_outputs(m, out)
        assert not stale.exists() and len(written) >= 3


# ── the drift monitor (fail-capable) ──────────────────────────────────────────
class TestDiff:
    def test_injected_fake_plus_removed_real_is_exactly_two_lines(self, tmp_path):
        prev = _norm(_raw(), tmp_path)
        cur = [copy.deepcopy(t) for t in prev if t["name"] != "cowork-cora-backup"]
        fake = copy.deepcopy(prev[0]); fake["name"] = "Cora - Injected Fake Task"
        cur.append(fake)
        lines = tem.warn_lines(tem.diff_manifests(prev, cur))
        assert lines == ["task-estate-drift: added Cora - Injected Fake Task",
                         "task-estate-drift: removed cowork-cora-backup"]

    def test_each_change_kind_has_its_own_line(self, tmp_path):
        prev = _norm(_raw(), tmp_path)
        cur = copy.deepcopy(prev)
        by = {t["name"]: t for t in cur}
        by["cowork-cora-backup"]["trigger_text"] = "daily 13:00"
        by["Cora - Log Compaction"]["enabled"] = True
        by["Cora - Weekly Health Metrics"]["action_text"] += " --extra"
        by["cowork-cora-service"]["run_as"]["run_level"] = "Highest"
        d = tem.diff_manifests(prev, cur)
        assert d["cron_changed"] == ["task-estate-drift: cron_changed cowork-cora-backup: daily 20:30 -> daily 13:00"]
        assert d["enabled_changed"] == ["task-estate-drift: enabled_changed Cora - Log Compaction: False -> True"]
        assert d["action_changed"] == ["task-estate-drift: action_changed Cora - Weekly Health Metrics"]
        assert d["principal_changed"] == ["task-estate-drift: principal_changed cowork-cora-service: Interactive/Limited/SWA=True -> Interactive/Highest/SWA=True"]
        assert d["added"] == [] and d["removed"] == [] and d["settings_changed"] == []
        assert len(tem.warn_lines(d)) == 4

    def test_volatile_fields_on_an_otherwise_untouched_task_are_silent(self, tmp_path):
        """The volatile mutations sit on a task with NO genuine change, so a regression that
        folded a volatile field into any comparison would surface as a line naming it."""
        prev = _norm(_raw(), tmp_path)
        cur = copy.deepcopy(prev)
        t = {x["name"]: x for x in cur}["cowork-cora-backup"]
        t["last_run_time"] = "2099-01-01T00:00:00"
        t["last_task_result"] = 1
        t["next_run_time"] = ""
        t["number_of_missed_runs"] = 7
        t["log"]["newest_mtime_utc"] = "2099-01-01T00:00:00Z"
        t["log"]["newest"] = "logs/tasks/cowork-cora-backup-2099-01-01.log"
        t["run_marker"]["last_ts"] = "2099-01-01T00:00:00+00:00"
        t["run_marker"]["present"] = False
        t["description"] = "changed description"
        assert tem.warn_lines(tem.diff_manifests(prev, cur)) == []

    def test_settings_changes_have_their_own_line(self, tmp_path):
        """The 2026-06-09 incident class: a shortened ExecutionTimeLimit killed the gmail sweep
        nightly for two weeks and nothing compared it."""
        prev = _norm(_raw(), tmp_path)
        cur = copy.deepcopy(prev)
        by = {t["name"]: t for t in cur}
        by["cowork-cora-backup"]["execution_time_limit"] = "PT10M"
        by["cowork-cora-service"]["restart_count"] = 0
        by["Cora - Weekly Health Metrics"]["run_as"]["user_id"] = "S-1-5-21-9-9-9-1002"
        d = tem.diff_manifests(prev, cur)
        assert len(d["settings_changed"]) == 3 and all(ln.startswith("task-estate-drift: settings_changed ") for ln in d["settings_changed"])
        assert any("cowork-cora-backup" in ln and "execution_time_limit=PT1H" in ln and "execution_time_limit=PT10M" in ln for ln in d["settings_changed"])
        assert len(tem.warn_lines(d)) == 3

    def test_identical_manifests_yield_no_lines(self, tmp_path):
        prev = _norm(_raw(), tmp_path)
        assert tem.warn_lines(tem.diff_manifests(prev, copy.deepcopy(prev))) == []

    def test_diff_against_committed_reads_only(self, tmp_path):
        tasks = _norm(_raw(), tmp_path)
        m = tem.build_manifest(tasks, host="fixture")
        out = tmp_path / "manifest"
        tem.write_outputs(m, out)
        before = (out / tem.MANIFEST_JSON).read_bytes()
        live = copy.deepcopy(tasks)
        live[0]["trigger_text"] = "at boot"
        r = tem.diff_against_committed(out, live_tasks=live)
        assert r["available"] and r["drifted"] and r["drift"]["cron_changed"] == 1
        assert r["manifest_count"] == 4 and r["live_count"] == 4
        assert (out / tem.MANIFEST_JSON).read_bytes() == before          # never writes
        assert tem.diff_against_committed(tmp_path / "nowhere")["available"] is False
        boom = tem.diff_against_committed(out, enumerate=lambda: (_ for _ in ()).throw(RuntimeError("no scheduler")))
        assert boom["available"] is False and "no scheduler" in boom["reason"]


# ── rendering + generated blocks ──────────────────────────────────────────────
class TestRender:
    def test_generated_blocks_are_idempotent_and_marker_gated(self, tmp_path):
        m = tem.build_manifest(_norm(_raw(), tmp_path), host="fixture")
        b1, b2 = tem.render_bootstrap_block(m), tem.render_bootstrap_block(m)
        assert b1 == b2 and "cowork-cora-backup" in b1 and "(none -- manifest XML only)" in b1     # Log Compaction has no setup script: its XML is the only restore form
        assert tem.render_runbook_table(m) == tem.render_runbook_table(m)
        doc = "intro\n<!-- BEGIN GENERATED: scheduled-estate -->\nold\n<!-- END GENERATED: scheduled-estate -->\noutro\n"
        once = tem.replace_generated_block(doc, "scheduled-estate", b1)
        twice = tem.replace_generated_block(once, "scheduled-estate", b1)
        assert once == twice and once.startswith("intro\n") and once.endswith("\noutro\n") and "old" not in once
        with pytest.raises(ValueError):
            tem.replace_generated_block("no markers here", "scheduled-estate", b1)
        with pytest.raises(ValueError):
            tem.replace_generated_block(doc + doc, "scheduled-estate", b1)

    def test_update_docs_rewrites_only_the_block(self, tmp_path):
        m = tem.build_manifest(_norm(_raw(), tmp_path), host="fixture")
        bs = tmp_path / "bootstrap.md"; rb = tmp_path / "runbook.md"
        bs.write_text("# B\n<!-- BEGIN GENERATED: scheduled-estate -->\nx\n<!-- END GENERATED: scheduled-estate -->\ntail\n", encoding="utf-8")
        rb.write_text("# R\n<!-- BEGIN GENERATED: task-registry -->\ny\n<!-- END GENERATED: task-registry -->\n", encoding="utf-8")
        changed = tem.update_docs(m, bootstrap=bs, runbook=rb)
        assert len(changed) == 2
        assert tem.update_docs(m, bootstrap=bs, runbook=rb) == []              # second run: byte-identical
        assert bs.read_text(encoding="utf-8").startswith("# B\n") and bs.read_text(encoding="utf-8").endswith("tail\n")

    def test_markdown_table_has_a_row_per_task_and_the_cross_check(self, tmp_path):
        m = tem.build_manifest(_norm(_raw(), tmp_path), host="fixture",
                               agreement={"cim_count": 4, "agree": False, "methods": {"schtasks_csv": {"available": True, "count": 3, "agree": False, "only_in_cim": ["x"], "only_in_method": []}}})
        md = tem.render_markdown(m)
        assert md.count("\n| `") == 4 and "DISAGREE" in md and "only in CIM: ['x']" in md
        assert "4 tasks" in md.splitlines()[0]


# ── the committed artifact ────────────────────────────────────────────────────
class TestCommittedManifest:
    def test_committed_manifest_is_well_formed_and_secret_free(self):
        assert COMMITTED.exists(), "deployment/manifest/task-estate.json must be committed (M1)"
        m = json.loads(COMMITTED.read_text(encoding="utf-8"))
        assert m["schema_version"] == tem.SCHEMA_VERSION
        assert m["count"] == len(m["tasks"]) >= 90
        names = [t["name"] for t in m["tasks"]]
        assert names == sorted(names, key=str.lower) and len(set(names)) == len(names)
        assert all(tem.name_matches(n) for n in names)
        ag = m["enumeration_agreement"]
        assert ag["cim_count"] == m["count"] and set(ag["methods"]) == {"schtasks_csv", "cora_health_view"}
        for t in m["tasks"]:
            for key in ("trigger_text", "child_command", "run_as", "start_when_available", "enabled", "state",
                        "last_task_result", "run_marker", "log", "intent", "ladder_lane", "setup_scripts"):
                assert key in t, (t["name"], key)
            assert "trigger from" not in t["trigger_text"], (t["name"], t["trigger_text"])   # restorable schedules only
        text = COMMITTED.read_text(encoding="utf-8")
        for rx in _SECRET_SHAPES:
            assert not rx.search(text), f"secret shape {rx.pattern} in the committed manifest"
        rm = m["run_marker_coverage"]
        assert rm["tasks_total"] == m["count"] and 0 <= rm["tasks_with_marker"] <= rm["tasks_total"]
        assert m["cowork_estate"]["migrates"] is False
        # D-051 lens B: the Cowork task ids (personal + staff-named) never enter the committed file
        assert "task_ids" not in m["cowork_estate"] and "_task_xml" not in m
        assert "task_ids_sha256_16" in m["cowork_estate"] or not m["cowork_estate"].get("available")
        # the restore form: one XML per task, the host time zone recorded (clock triggers are host-local)
        assert m.get("host_time_zone"), "manifest must record the host time zone"
        xml_dir = COMMITTED.parent / tem.TASK_XML_SUBDIR
        xml_slugs = {p.stem for p in xml_dir.glob("*.xml")}
        assert xml_slugs == {t["log_slug"] for t in m["tasks"]}, "deployment/manifest/tasks/*.xml must cover exactly the manifest tasks"
        for t in m["tasks"]:
            assert "_xml" not in t

    def test_committed_docs_carry_the_generated_blocks(self):
        bs = (REPO / "deployment" / "bootstrap-new-machine.md").read_text(encoding="utf-8")
        rb = (REPO / "deployment" / "runbook.md").read_text(encoding="utf-8")
        m = json.loads(COMMITTED.read_text(encoding="utf-8"))
        for doc, block in ((bs, tem.BOOTSTRAP_BLOCK), (rb, tem.RUNBOOK_BLOCK)):
            begin, end = tem._markers(block)  # noqa: SLF001
            assert doc.count(begin) == 1 and doc.count(end) == 1
            body = doc.split(begin, 1)[1].split(end, 1)[0]
            for t in m["tasks"]:
                assert f"`{t['name']}`" in body, (block, t["name"])
        # the generated blocks are exactly what the committed manifest renders (no hand edits)
        assert tem.render_bootstrap_block(m).strip() == bs.split(tem._markers(tem.BOOTSTRAP_BLOCK)[0], 1)[1].split(tem._markers(tem.BOOTSTRAP_BLOCK)[1], 1)[0].strip()  # noqa: SLF001
        assert tem.render_runbook_table(m).strip() == rb.split(tem._markers(tem.RUNBOOK_BLOCK)[0], 1)[1].split(tem._markers(tem.RUNBOOK_BLOCK)[1], 1)[0].strip()  # noqa: SLF001


# ── the Monday digest reader ──────────────────────────────────────────────────
class TestHealthReportSection:
    def test_section_is_fail_soft_and_alarms_only_on_drift(self, monkeypatch):
        import cora_health_report as chr_  # noqa: PLC0415
        monkeypatch.setattr(tem, "diff_against_committed", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        s = chr_.task_estate_section()
        assert s["available"] is False and "boom" in s["reason"]
        assert not [a for a in chr_.threshold_alarms({"task_estate": s}) if "TASK-ESTATE DRIFT" in a]
        assert any("TASK-ESTATE manifest check unavailable" in a for a in chr_.threshold_alarms({"task_estate": s}))
        clean = {"available": True, "manifest_count": 96, "live_count": 96, "drift": {}, "warn_lines": [], "drifted": False}
        assert not [a for a in chr_.threshold_alarms({"task_estate": clean}) if "TASK-ESTATE" in a]
        drifted = dict(clean, drifted=True, warn_lines=["task-estate-drift: added X", "task-estate-drift: removed Y"],
                       drift={"added": 1, "removed": 1})
        alarms = [a for a in chr_.threshold_alarms({"task_estate": drifted}) if "TASK-ESTATE DRIFT" in a]
        assert len(alarms) == 1 and "added X" in alarms[0] and "removed Y" in alarms[0]
        line = chr_._task_estate_digest_line(drifted)  # noqa: SLF001
        assert "96 live / 96 manifest" in line and "2 drift" in line


# ── live host (Windows only) ──────────────────────────────────────────────────
def test_failed_second_method_reads_unavailable_not_disagree(monkeypatch):
    """schtasks failing (rc != 0 / no rows) must not be recorded as 'DISAGREE: 0 tasks'."""
    class _P:  # noqa: D401
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""
    monkeypatch.setattr(tem.subprocess, "run", lambda *a, **k: _P(1, ""))
    assert tem.enumerate_schtasks_csv() is None
    monkeypatch.setattr(tem.subprocess, "run", lambda *a, **k: _P(0, ""))
    assert tem.enumerate_schtasks_csv() is None
    ag = tem.enumeration_agreement({"cowork-cora-service"}, None, set())
    assert ag["methods"]["schtasks_csv"] == {"available": False, "reason": "call failed"}
    assert ag["methods"]["cora_health_view"]["available"] is False and ag["agree"] is True


@pytest.mark.skipif(os.name != "nt", reason="Task Scheduler is Windows-only")
def test_live_enumeration_records_three_methods():
    raw = tem.enumerate_cim()
    if not raw:
        pytest.skip("no Cora estate registered on this host (run after bootstrap Phase 5)")
    names = {r["name"] for r in raw}
    assert all(tem.name_matches(n) for n in names)
    ag = tem.enumeration_agreement(names, tem.enumerate_schtasks_csv(), tem.enumerate_health_view())
    assert ag["cim_count"] == len(names)
    assert ag["methods"]["schtasks_csv"]["available"] is True
    # agreement OR a named disagreement -- never silence
    for r in ag["methods"].values():
        if r.get("available"):
            assert "agree" in r and "only_in_cim" in r and "only_in_method" in r
