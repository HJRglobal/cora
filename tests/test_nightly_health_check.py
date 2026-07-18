"""Tests for the nightly health-check scheduled-task classifier + state config.

Audit N8: the check fired a daily false CRITICAL because the hardcoded
expected-disabled set was stale (listed the now-ENABLED Meeting Action Capture,
omitted the disabled cowork-clover-daily-pull). The intended state now lives in
data/maps/scheduled-task-state.yaml, and a disabled-state drift is a WARNING --
only the always-on service being down is CRITICAL.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import nightly_health_check as hc  # noqa: E402


# ── config loader / D-052 pin ────────────────────────────────────────────────

def test_state_config_loads():
    disabled, running = hc._load_task_state_config()
    assert "cowork-cora-service" in running
    # "Cora - Clover Daily Summary" was REMOVED from the host (audit W4-03/W8-03,
    # 2026-07-03); a removed task is not an expected-disabled task, so it must NOT
    # be listed. cowork-clover-daily-pull is still present-but-Disabled, so it stays.
    assert "Cora - Clover Daily Summary" not in disabled
    assert "cowork-clover-daily-pull" in disabled  # was missing -> false CRITICAL
    assert "cowork-cora-digest" in disabled  # WS17-C: silences the daily false WARN
    assert "cowork-cora-gap-digest" in disabled  # retired 2026-07-02 (hygiene S1)


def test_meeting_action_capture_now_intended_disabled():
    # 2026-06-18 push -> pull: the auto-create "Cora - Meeting Action Capture"
    # task is RETIRED (disabled); meeting items are created on request via the
    # meeting_action_items pull tool. It is now an EXPECTED-disabled task, so the
    # nightly check finds no drift once Harrison runs the disable script.
    # (Supersedes the D-052 "must NOT be listed as intended-disabled" pin.)
    disabled, _ = hc._load_task_state_config()
    assert "Cora - Meeting Action Capture" in disabled
    assert "Cora - Meeting Action Capture" in hc._EXPECTED_DISABLED


# ── classifier severity (drift = warn, service-down = critical) ──────────────

_DIS = {"cowork-cora-asana-email-sync"}
_RUN = {"cowork-cora-service"}


def test_all_expected_states_ok():
    states = {
        "cowork-cora-service": "Running",
        "cowork-cora-asana-email-sync": "Disabled",
        "Cora - Daily Briefing": "Ready",
    }
    crit, warn, ok = hc._classify_task_states(states, _DIS, _RUN)
    assert crit == [] and warn == [] and ok == 3


def test_intended_disabled_found_enabled_is_warn_not_critical():
    crit, warn, ok = hc._classify_task_states(
        {"cowork-cora-asana-email-sync": "Ready"}, _DIS, _RUN
    )
    assert crit == []
    assert len(warn) == 1 and "asana-email-sync" in warn[0]


def test_unexpectedly_disabled_is_warn_not_critical():
    crit, warn, ok = hc._classify_task_states(
        {"Cora - Daily Briefing": "Disabled"}, _DIS, _RUN
    )
    assert crit == []
    assert len(warn) == 1 and "unexpectedly Disabled" in warn[0]


def test_service_down_is_critical():
    crit, warn, ok = hc._classify_task_states(
        {"cowork-cora-service": "Ready"}, _DIS, _RUN
    )
    assert len(crit) == 1 and "expected Running" in crit[0]
    assert warn == []


def test_meeting_action_capture_ready_is_ok():
    # Removed from intended-disabled, an enabled MAC is just OK (no false CRITICAL).
    crit, warn, ok = hc._classify_task_states(
        {"Cora - Meeting Action Capture": "Ready"}, _DIS, _RUN
    )
    assert crit == [] and warn == [] and ok == 1


# ── QBO token-monitor freshness meta-check (B5/#5, 2026-06-17) ────────────────
from datetime import datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def _mock_schtasks(monkeypatch, returncode, last_run=None):
    stdout = ""
    if last_run is not None:
        stdout = (f"TaskName: \\Cora - QBO Token Monitor\n"
                  f"Last Run Time: {last_run}\nLast Result: 0\nStatus: Ready\n")
    monkeypatch.setattr(
        hc.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=returncode, stdout=stdout, stderr=""))


def test_qbo_monitor_missing_is_warn(monkeypatch):
    _mock_schtasks(monkeypatch, returncode=1)
    r = hc.check_qbo_monitor()
    assert r.status == "warn" and "not registered" in r.detail


def test_qbo_monitor_recent_is_ok(monkeypatch):
    _mock_schtasks(monkeypatch, 0, last_run="6/18/2026 6:50:00 AM")
    r = hc.check_qbo_monitor(now=datetime(2026, 6, 18, 8, 0, 0))
    assert r.status == "ok"


def test_qbo_monitor_stale_is_warn(monkeypatch):
    _mock_schtasks(monkeypatch, 0, last_run="6/18/2026 6:50:00 AM")
    r = hc.check_qbo_monitor(now=datetime(2026, 6, 20, 8, 0, 0))  # ~49h later
    assert r.status == "warn" and "stopped firing" in r.detail


def test_qbo_monitor_never_run_is_warn(monkeypatch):
    _mock_schtasks(monkeypatch, 0, last_run="N/A")
    r = hc.check_qbo_monitor()
    assert r.status == "warn" and "never run" in r.detail


# ── Dynamic-answers snapshot freshness (D-084) ────────────────────────────────
import time as _time  # noqa: E402


def _setup_dynamic(monkeypatch, tmp_path):
    dyn = tmp_path / "design" / "known-answers" / "dynamic"
    dyn.mkdir(parents=True)
    monkeypatch.setattr(hc, "_DYNAMIC_ANSWERS_DIR", dyn)
    monkeypatch.setattr(hc, "_REPO_ROOT", tmp_path)
    return dyn


def _write_dyn_yaml(dyn, entity, name, snap_rel, threshold_hours=336):
    ed = dyn / entity
    ed.mkdir(exist_ok=True)
    (ed / name).write_text(
        f"topic: T\nsnapshot_path: {snap_rel}\nsource:\n"
        f"  staleness_threshold_hours: {threshold_hours}\n",
        encoding="utf-8",
    )


def test_dynamic_snapshots_fresh_is_ok(monkeypatch, tmp_path):
    dyn = _setup_dynamic(monkeypatch, tmp_path)
    _write_dyn_yaml(dyn, "F3E", "pipeline.yaml", "data/snap/p.yaml")
    snap = tmp_path / "data" / "snap" / "p.yaml"
    snap.parent.mkdir(parents=True)
    snap.write_text("x", encoding="utf-8")
    r = hc.check_dynamic_snapshots(now_epoch=_time.time())
    assert r.status == "ok" and "1 dynamic snapshot" in r.detail


def test_dynamic_snapshots_stale_is_warn(monkeypatch, tmp_path):
    dyn = _setup_dynamic(monkeypatch, tmp_path)
    _write_dyn_yaml(dyn, "F3E", "pipeline.yaml", "data/snap/p.yaml", threshold_hours=336)
    snap = tmp_path / "data" / "snap" / "p.yaml"
    snap.parent.mkdir(parents=True)
    snap.write_text("x", encoding="utf-8")
    old = _time.time() - 1400 * 3600  # the real ~58d staleness observed 2026-07-17
    import os as _os
    _os.utime(snap, (old, old))
    r = hc.check_dynamic_snapshots(now_epoch=_time.time())
    assert r.status == "warn"
    assert "stale/missing" in r.detail and "serving the yaml fallback" in r.detail


def test_dynamic_snapshots_missing_is_warn(monkeypatch, tmp_path):
    dyn = _setup_dynamic(monkeypatch, tmp_path)
    _write_dyn_yaml(dyn, "FNDR", "cash.yaml", "data/snap/does-not-exist.yaml")
    r = hc.check_dynamic_snapshots(now_epoch=_time.time())
    assert r.status == "warn" and "MISSING" in r.detail


def test_dynamic_snapshots_no_dir_is_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(hc, "_DYNAMIC_ANSWERS_DIR", tmp_path / "nope")
    r = hc.check_dynamic_snapshots()
    assert r.status == "ok"


def test_dynamic_snapshots_empty_dir_is_ok(monkeypatch, tmp_path):
    # D-085 retirement: after the 4 manual seeds were removed, the dynamic dir still
    # EXISTS (entity subfolders keep their .gitkeep) but holds zero *.yaml seeds.
    # That zero-seed state must green -- the health check must NOT regress to
    # WARN-on-empty now that there is nothing to be stale.
    dyn = _setup_dynamic(monkeypatch, tmp_path)
    for e in ("FNDR", "F3E", "OSN"):  # empty subdirs, mirroring the kept .gitkeep dirs
        (dyn / e).mkdir()
    r = hc.check_dynamic_snapshots(now_epoch=_time.time())
    assert r.status == "ok"
    assert "0 dynamic snapshot" in r.detail


def test_dynamic_snapshots_malformed_source_does_not_raise(monkeypatch, tmp_path):
    # D-051 [4]: a non-dict `source` (or bad threshold) must be fail-soft, never abort
    # the whole nightly report.
    dyn = _setup_dynamic(monkeypatch, tmp_path)
    ed = dyn / "F3E"
    ed.mkdir()
    (ed / "bad.yaml").write_text(
        "snapshot_path: data/snap/x.yaml\nsource: not-a-dict\n", encoding="utf-8")
    r = hc.check_dynamic_snapshots(now_epoch=_time.time())  # must not raise
    assert r.status in ("ok", "warn")


# ── Founder CLAUDE.md KB freshness (D-084 / D-051 [3]) ────────────────────────
def _make_founder_kb(tmp_path, ingested_at, count=5, source_id="CLAUDE.md"):
    import sqlite3
    p = tmp_path / "kb.db"
    c = sqlite3.connect(str(p))
    c.execute("CREATE TABLE knowledge_chunks "
              "(entity TEXT, source TEXT, source_id TEXT, ingested_at REAL)")
    for _ in range(count):
        c.execute("INSERT INTO knowledge_chunks VALUES ('FNDR','static_md',?,?)",
                  (source_id, ingested_at))
    c.commit()
    c.close()
    return p


def test_founder_kb_fresh_is_ok(monkeypatch, tmp_path):
    now = _time.time()
    monkeypatch.setattr(hc, "_KB_DB", _make_founder_kb(tmp_path, now - 3600))
    r = hc.check_founder_kb_freshness(now_epoch=now)
    assert r.status == "ok"


def test_founder_kb_stale_is_warn(monkeypatch, tmp_path):
    now = _time.time()
    monkeypatch.setattr(hc, "_KB_DB", _make_founder_kb(tmp_path, now - 40 * 3600))
    r = hc.check_founder_kb_freshness(now_epoch=now)
    assert r.status == "warn" and "static_md sweep" in r.detail


def test_founder_kb_missing_is_warn(monkeypatch, tmp_path):
    now = _time.time()
    monkeypatch.setattr(hc, "_KB_DB", _make_founder_kb(tmp_path, now, count=0))
    r = hc.check_founder_kb_freshness(now_epoch=now)
    assert r.status == "warn" and "NOT indexed" in r.detail


# ── W4-07: LastTaskResult classifier ──────────────────────────────────────────

_BENIGN = hc._BENIGN_LAST_RESULTS
_SIGNAL_OK = hc._LASTRESULT_SIGNAL_OK


def _classify(results):
    return hc._classify_task_last_results(results, _BENIGN, _SIGNAL_OK)


def test_lastresult_success_and_status_codes_are_ok():
    warn, ok = _classify({
        "cowork-cora-kb-sync-slack": ("Ready", 0),          # success
        "cowork-cora-service": ("Running", 267009),          # RUNNING
        "cowork-cora-kb-evals": ("Ready", 267011),           # HAS_NOT_RUN
        "cowork-cora-x": ("Ready", 267008),                  # READY
    })
    assert warn == [] and ok == 4


def test_lastresult_task_terminated_warns():
    # W4-01 founders-os-sweep: 267014 TASK_TERMINATED on an enabled task.
    warn, ok = _classify({"cowork-cora-founders-os-sweep": ("Ready", 267014)})
    assert ok == 0 and len(warn) == 1
    assert "founders-os-sweep" in warn[0] and "267014" in warn[0]


def test_lastresult_generic_failure_warns():
    # W4-02 finance-receipt-digest: exit 1 on an enabled task.
    warn, ok = _classify({"cowork-cora-finance-receipt-digest": ("Ready", 1)})
    assert ok == 0 and len(warn) == 1 and "finance-receipt-digest" in warn[0]


def test_lastresult_disabled_task_skipped_even_if_nonzero():
    # The adversarial concern: a legitimately-DISABLED task with a stale nonzero
    # result must NOT false-alarm.
    warn, ok = _classify({"cowork-cora-clover-daily-pull": ("Disabled", 267014)})
    assert warn == [] and ok == 0


def test_lastresult_qbo_monitor_signal_exit_allowlisted():
    # Documented nonzero-as-signal (covered by check_qbo_monitor).
    warn, ok = _classify({"Cora - QBO Token Monitor": ("Ready", 1)})
    assert warn == [] and ok == 1


def test_lastresult_health_check_self_excluded():
    warn, ok = _classify({"cowork-cora-health-check": ("Ready", 1)})
    assert warn == [] and ok == 1


def test_lastresult_unreadable_none_never_warns():
    warn, ok = _classify({"cowork-cora-x": ("Ready", None)})
    assert warn == [] and ok == 0


def test_lastresult_mixed_fleet_flags_only_real_failures():
    warn, ok = _classify({
        "cowork-cora-founders-os-sweep": ("Ready", 267014),        # WARN
        "cowork-cora-finance-receipt-digest": ("Ready", 1),        # WARN
        "cowork-cora-kb-sync-slack": ("Ready", 0),                 # ok
        "cowork-cora-clover-daily-pull": ("Disabled", 267014),     # skipped
        "Cora - QBO Token Monitor": ("Ready", 1),                  # allow-listed
    })
    assert ok == 2
    assert len(warn) == 2
    names = " ".join(warn)
    assert "founders-os-sweep" in names and "finance-receipt-digest" in names


def test_get_task_last_results_parses_pipe_output(monkeypatch):
    fake = (
        "cowork-cora-service|Running|267009\r\n"
        "cowork-cora-founders-os-sweep|Ready|267014\r\n"
        "Cora - QBO Token Monitor|Ready|1\r\n"
        "cowork-cora-clover-daily-pull|Disabled|267014\r\n"
    )
    from types import SimpleNamespace
    monkeypatch.setattr(hc.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=fake, returncode=0))
    parsed = hc._get_task_last_results()
    assert parsed["cowork-cora-founders-os-sweep"] == ("Ready", 267014)
    assert parsed["Cora - QBO Token Monitor"] == ("Ready", 1)
    assert parsed["cowork-cora-service"] == ("Running", 267009)


def test_check_task_last_results_empty_query_is_soft_warn(monkeypatch):
    monkeypatch.setattr(hc, "_get_task_last_results", lambda: {})
    results = hc.check_task_last_results()
    assert len(results) == 1 and results[0].status == "warn"


def test_check_task_last_results_all_clean_is_ok(monkeypatch):
    monkeypatch.setattr(hc, "_get_task_last_results",
                        lambda: {"cowork-cora-x": ("Ready", 0)})
    results = hc.check_task_last_results()
    assert len(results) == 1 and results[0].status == "ok"
