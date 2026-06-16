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


def test_meeting_action_capture_not_intended_disabled():
    # D-052: Meeting Action Capture is ENABLED. Listing it caused a daily false
    # CRITICAL ("expected Disabled, got Ready").
    disabled, _ = hc._load_task_state_config()
    assert "Cora - Meeting Action Capture" not in disabled
    assert "Cora - Meeting Action Capture" not in hc._EXPECTED_DISABLED


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
