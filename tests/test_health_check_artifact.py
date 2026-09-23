"""Code #14 S2 (cq-0f04ad6543a8): the nightly health check's durable run artifact.

THE DEFECT. main() logged every result as r.detail[:100], so the task log cut each
detail mid-word ("... task-estate added: fndr-notetak") and the Slack post was the
only full read of record. S2 logs the FULL detail (flattened to one line) and writes
reports/health/YYYY-MM-DD.json + a .md twin on every real run.

THE TRAP VERIFY-FIRST FOUND. check_logs_24h scans logs/*.log, which includes the
health check's OWN log. The "Critical log patterns" detail quotes the matched line,
so logging it in full would make the next morning's scan re-match the quote and
re-raise the same CRITICAL forever. The fix skips the check's own REPORT lines in
its own log -- narrowly, so a real writer failure raised inside this process
(REPEAT_SIGNAL_WRITE_FAILING) is still caught.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import compact_logs  # noqa: E402
import nightly_health_check as hc  # noqa: E402

PIN = datetime(2026, 9, 22, 8, 45, 10)  # naive AZ wall clock, like the host


def _results() -> list:
    CR = hc.CheckResult
    return [
        CR("Cora heartbeat", "ok", "Heartbeat 42s ago."),
        CR("Claude mirror", "warn",
           "claude-workspace mirror: skills not allowlisted: docs, setup-claude; "
           "task-estate added: fndr-notetaker-sweep, and a much longer tail " + "x" * 300),
        CR("Critical log patterns", "critical",
           "1 critical pattern(s) found:\n  • [cora-2026-08-26.log] 2026-08-28T03:25:37 "
           "CRITICAL cora: FATAL shutdown — em dash"),
        CR("Cora restart", "fixed", "Heartbeat was stale.", "orphan-kill + restart"),
        CR("Egress rails (observe week)", "ok",
           "sentinel-egress-leak 0 (24h) / 0 (7d) | phantom-write-claim 0 (24h) / 0 (7d)"),
    ]


# ── the artifact shape ───────────────────────────────────────────────────────


def test_artifact_round_trips_to_the_exact_results_and_report():
    orig = _results()
    report = hc._build_report(orig, 8.8, now=PIN)
    art = hc._build_artifact(orig, 8.8, exit_code=1, dry_run=False,
                             report_text=report, now=PIN)
    back = json.loads(json.dumps(art, ensure_ascii=False))
    rebuilt = [hc.CheckResult(**c) for c in back["checks"]]
    assert rebuilt == orig
    assert hc._build_report(rebuilt, back["run_time_s"], now=PIN) == report
    assert back["report_text"] == report
    assert back["schema"] == 1
    assert back["run_ts"] == "2026-09-22T08:45:10-07:00"
    assert back["exit_code"] == 1 and back["dry_run"] is False
    assert back["counts"] == {"critical": 1, "warn": 1, "fixed": 1, "ok": 2}


def test_artifact_carries_heartbeat_and_rails_line():
    art = hc._build_artifact(_results(), 1.0, exit_code=0, dry_run=False,
                             report_text="r", now=PIN)
    assert art["heartbeat"] == {"status": "ok", "detail": "Heartbeat 42s ago."}
    assert art["rails_line"].startswith("Egress rails (observe week) [ok]: sentinel-egress-leak")
    bare = hc._build_artifact([hc.CheckResult("x", "ok", "y")], 1.0, exit_code=0,
                              dry_run=False, report_text="r", now=PIN)
    assert bare["heartbeat"] is None and bare["rails_line"] is None


def test_build_report_now_param_pins_the_clock_and_keeps_the_format():
    """The default path (no now=) is what main() posts; now= only pins the stamp.
    A golden string guards the posted format against drift."""
    out = hc._build_report([hc.CheckResult("A", "ok", "fine")], 2.04, now=PIN)
    assert out == (":white_check_mark: *Cora Health Check — 2026-09-22 08:45 AZ*\n"
                   "*Summary:* 0 critical · 0 warning · 0 auto-fixed · 1 OK  _(ran in 2.0s)_\n"
                   "\n_All systems healthy. Nothing to fix._")


# ── the writer ───────────────────────────────────────────────────────────────


def test_write_artifact_creates_dir_json_and_md_twin(tmp_path):
    out = tmp_path / "deep" / "health"
    report = hc._build_report(_results(), 3.0, now=PIN)
    art = hc._build_artifact(_results(), 3.0, exit_code=1, dry_run=False,
                             report_text=report, now=PIN)
    path = hc._write_artifact(art, out)
    assert path == out / "2026-09-22.json"
    assert json.loads(path.read_text(encoding="utf-8")) == art
    md = (out / "2026-09-22.md").read_text(encoding="utf-8")
    assert "x" * 300 in md                                     # the FULL detail
    assert "sentinel-egress-leak 0 (24h) / 0 (7d) \\| phantom" in md  # pipes escaped
    assert "found:<br>  • [cora-2026-08-26.log]" in md         # newlines folded in-cell
    assert report in md
    assert not list(out.glob("*.tmp"))                         # atomic: no leftovers
    # a same-day real re-run overwrites (last run wins)
    art2 = dict(art, exit_code=0)
    hc._write_artifact(art2, out)
    assert json.loads(path.read_text(encoding="utf-8"))["exit_code"] == 0


def test_write_failure_is_fail_soft(tmp_path, caplog):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file", encoding="utf-8")
    art = hc._build_artifact(_results(), 1.0, exit_code=0, dry_run=False,
                             report_text="r", now=PIN)
    with caplog.at_level(logging.WARNING, logger="health-check"):
        assert hc._write_artifact(art, blocker / "health") is None
    assert "artifact write failed" in caplog.text


def test_report_dir_is_resolved_per_call_and_redirected_by_conftest(monkeypatch, tmp_path):
    assert str(tmp_path) in os.environ["CORA_HEALTH_REPORT_DIR"]
    assert hc._health_report_dir() == Path(os.environ["CORA_HEALTH_REPORT_DIR"])
    monkeypatch.setenv("CORA_HEALTH_REPORT_DIR", str(tmp_path / "elsewhere"))
    assert hc._health_report_dir() == tmp_path / "elsewhere"
    monkeypatch.delenv("CORA_HEALTH_REPORT_DIR")
    assert hc._health_report_dir() == _REPO_ROOT / "reports" / "health"
    assert compact_logs._health_report_dir() == _REPO_ROOT / "reports" / "health"


def test_reports_dir_is_gitignored():
    lines = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/reports/" in lines


# ── main(): dry run writes nothing, real run writes + posts byte-identically ──


class _FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return PIN if tz is None else PIN.replace(tzinfo=hc._AZ).astimezone(tz)


def _stub_checks(monkeypatch, results: list):
    """Every module-level check_* returns nothing but `results` (served once, by
    check_heartbeat); list-returning checks return []."""
    listy = ("check_scheduled_tasks", "check_task_last_results", "check_logs_24h",
             "check_kb_health", "check_api_connectivity", "check_flywheel")
    for name in dir(hc):
        if not name.startswith("check_") or not callable(getattr(hc, name)):
            continue
        if name == "check_heartbeat":
            monkeypatch.setattr(hc, name, lambda *a, **k: results[0])
        elif name == "check_logs_24h":
            monkeypatch.setattr(hc, name, lambda *a, **k: list(results[1:]))
        elif name in listy:
            monkeypatch.setattr(hc, name, lambda *a, **k: [])
        else:
            monkeypatch.setattr(hc, name, lambda *a, _n=name, **k: hc.CheckResult(_n, "ok", "stub"))


def _run_main(monkeypatch, tmp_path, argv, results):
    posted: list[str] = []
    _stub_checks(monkeypatch, results)
    monkeypatch.setattr(hc, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(hc, "_post_to_slack", lambda msg, *a, **k: posted.append(msg))
    monkeypatch.setattr(hc.logging, "basicConfig", lambda **_k: None)
    monkeypatch.setattr(hc, "datetime", _FixedDT)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(sys, "argv", ["nightly_health_check.py", *argv])
    rc = hc.main()
    return rc, posted


def test_dry_run_writes_no_artifact(monkeypatch, tmp_path, capfd):
    out = Path(os.environ["CORA_HEALTH_REPORT_DIR"])
    rc, posted = _run_main(monkeypatch, tmp_path, ["--dry-run"], _results())
    assert rc == 0 and posted == []
    assert not out.exists() or not any(out.iterdir())
    assert "would write" in capfd.readouterr().out


def test_real_run_writes_artifact_and_the_post_is_byte_identical(monkeypatch, tmp_path):
    out = Path(os.environ["CORA_HEALTH_REPORT_DIR"])
    rc, posted = _run_main(monkeypatch, tmp_path, [], _results())
    assert rc == 1                                            # a critical is present
    art = json.loads((out / "2026-09-22.json").read_text(encoding="utf-8"))
    assert art["exit_code"] == 1 and art["dry_run"] is False
    assert len(posted) == 1
    assert posted[0] == art["report_text"]
    rebuilt = [hc.CheckResult(**c) for c in art["checks"]]
    assert posted[0] == hc._build_report(rebuilt, art["run_time_s"], now=PIN)
    assert (out / "2026-09-22.md").exists()


def test_artifact_write_failure_never_blocks_the_post(monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    monkeypatch.setenv("CORA_HEALTH_REPORT_DIR", str(blocker / "health"))
    rc, posted = _run_main(monkeypatch, tmp_path, [], _results())
    assert rc == 1 and len(posted) == 1


def test_log_line_carries_the_full_flattened_detail(monkeypatch, tmp_path, caplog):
    long_detail = "first line\n" + "y" * 450 + "\nlast-token-visible"
    results = [hc.CheckResult("Cora heartbeat", "warn", long_detail)]
    with caplog.at_level(logging.INFO, logger="health-check"):
        _run_main(monkeypatch, tmp_path, ["--dry-run"], results)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[WARN] Cora heartbeat")]
    assert lines == ["[WARN] Cora heartbeat: first line | " + "y" * 450 + " | last-token-visible"]


# ── check_logs_24h: its own report lines never re-raise themselves ──────────


def _now_stamp(minutes_ago: int = 5) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S,000")


def _crit(results):
    return next(r for r in results if r.name == "Critical log patterns")


def test_own_log_report_lines_are_not_rescanned(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    own = [
        # the verbatim 8/28 two-line shape (stamped report line out of window, its
        # unstamped continuation KEPT by _line_within)
        "2026-08-28 08:45:07,698 INFO health-check: [CRITICAL] Critical log patterns: 1 critical pattern(s) found:",
        "  • [cora-2026-08-26.log] 2026-08-28T03:25:37 WARNING [HealthPing] cora",
        # the same shape where the quoted snippet DOES match a critical pattern
        "2026-08-28 08:45:07,698 INFO health-check: [CRITICAL] Critical log patterns: 1 critical pattern(s) found:",
        "  • [cora-2026-08-26.log] 2026-08-28T03:25:37 CRITICAL cora: FATAL shutdown",
        # what S2's full-detail logging now writes, in-window, flattened to one line
        f"{_now_stamp()} INFO health-check: [CRITICAL] Critical log patterns: 1 critical "
        "pattern(s) found: |   • [cora-x.log] 2026-09-22T03:25:37 CRITICAL cora: FATAL shutdown",
        f"{_now_stamp()} INFO health-check: [WARN] Claude mirror: connection refused by the mirror probe",
    ]
    (logs / "health-check-2026-09-22.log").write_text("\n".join(own) + "\n", encoding="utf-8")
    monkeypatch.setattr(hc, "_LOG_DIR", logs)
    assert _crit(hc.check_logs_24h()).status == "ok"


def test_a_real_in_process_writer_failure_in_the_own_log_is_still_critical(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "health-check-2026-09-22.log").write_text(
        f"{_now_stamp()} INFO health-check: [OK] Decision gates: fine\n"
        f"{_now_stamp()} ERROR cora.repeat_signal: REPEAT_SIGNAL_WRITE_FAILING could not append\n",
        encoding="utf-8")
    monkeypatch.setattr(hc, "_LOG_DIR", logs)
    res = hc.check_logs_24h()
    assert _crit(res).status == "critical"
    assert "REPEAT_SIGNAL_WRITE_FAILING" in _crit(res).detail
    vol = next(r for r in res if r.name == "Log error volume")
    assert vol.detail.startswith("1 ERROR(s)")                # ERROR tally unchanged


def test_the_same_report_text_in_a_bot_log_is_still_critical(monkeypatch, tmp_path):
    """Scoping is by FILENAME: only the check's own log is exempt."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "cora-2026-09-22.log").write_text(
        f"{_now_stamp()} INFO health-check: [CRITICAL] Critical log patterns: FATAL shutdown\n",
        encoding="utf-8")
    monkeypatch.setattr(hc, "_LOG_DIR", logs)
    assert _crit(hc.check_logs_24h()).status == "critical"


# ── retention: compact_logs.prune_reports ───────────────────────────────────


def _seed_reports(d: Path, today: date) -> dict[str, Path]:
    d.mkdir(parents=True, exist_ok=True)
    files = {}
    for age in (100, 91, 90, 5):
        day = (today - timedelta(days=age)).isoformat()
        for ext in ("json", "md"):
            p = d / f"{day}.{ext}"
            p.write_text("{}", encoding="utf-8")
            files[f"{age}.{ext}"] = p
    for other in ("notes.md", "2026-01-01.json.tmp", "README.txt"):
        p = d / other
        p.write_text("keep", encoding="utf-8")
        files[other] = p
    return files


def test_prune_reports_keeps_90_days_and_honours_dry_run(tmp_path):
    today = date(2026, 9, 22)
    d = tmp_path / "health"
    files = _seed_reports(d, today)
    dry = compact_logs.prune_reports(90, True, today=today, report_dir=d)
    assert dry["pruned"] == 4
    assert all(p.exists() for p in files.values())            # dry run deletes nothing
    real = compact_logs.prune_reports(90, False, today=today, report_dir=d)
    assert real["pruned"] == 4
    gone = {k for k, p in files.items() if not p.exists()}
    assert gone == {"100.json", "100.md", "91.json", "91.md"}


def test_prune_reports_missing_dir_is_a_noop(tmp_path):
    assert compact_logs.prune_reports(90, False, report_dir=tmp_path / "nope")["pruned"] == 0


def test_compact_logs_main_wires_the_prune_and_keeps_its_dry_run(monkeypatch, tmp_path, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(compact_logs, "LOGS_DIR", logs)
    monkeypatch.setattr(compact_logs, "ARCHIVE_DIR", logs / "archive")
    monkeypatch.setattr(compact_logs, "DATA_DIR", tmp_path / "data")
    d = Path(os.environ["CORA_HEALTH_REPORT_DIR"])
    files = _seed_reports(d, date.today())
    monkeypatch.setattr(sys, "argv", ["compact_logs.py", "--dry-run"])
    assert compact_logs.main() == 0
    assert all(p.exists() for p in files.values())
    assert "[dry-run] health reports: pruned 4" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["compact_logs.py"])
    assert compact_logs.main() == 0
    assert not files["100.json"].exists() and files["90.json"].exists()
