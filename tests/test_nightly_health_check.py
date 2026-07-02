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
    assert "Cora - Clover Daily Summary" in disabled
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
